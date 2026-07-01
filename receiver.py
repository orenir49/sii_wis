#!/usr/bin/env python
"""
SPAD Receiver — master control GUI.

Manages two sender nodes:
  - Control channel : receiver → sender command server (JSON commands)
  - Data channel    : sender → receiver data server   (binary chunks)

Workflow:
  1. Enter sender IP / ports / output folder per node, click Connect.
  2. Set duration and mode, click START ALL.
  3. Each connected sender runs its acquisition and streams data here.
"""

import csv
import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, simpledialog, messagebox

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from receiver_backend import start_server, check_connection, run_session_loop
from correlate import CorrelateWindow
from offset_tools import estimate_offset
import ssh_launcher

HEALTH_CHECK_MS = 2_000


# ---------------------------------------------------------------------------
# NodePanel — one sender node (control client + data server)
# ---------------------------------------------------------------------------

class NodePanel:
    def __init__(self, parent: tk.Widget, root: tk.Tk,
                 node_id: int,
                 default_sender_ip: str,
                 default_cmd_port: int,
                 default_data_port: int,
                 default_ssh_user: str = 'user',
                 log_fn=None,
                 get_hooks_fn=None) -> None:
        self.root          = root
        self.node_id       = node_id
        self.log_fn        = log_fn
        self._get_hooks_fn = get_hooks_fn

        self._ctrl_sock:   socket.socket | None = None
        self._data_server: socket.socket | None = None
        self._data_conn:   socket.socket | None = None
        self._ctrl_lock    = threading.Lock()
        self._state        = 'idle'   # 'idle' | 'ready' | 'streaming'
        self._dwell_q: queue.Queue = queue.Queue()
        self._ssh_creds: tuple | None = None       # (host, user, password) set after Launch
        self._shutdown_thread: threading.Thread | None = None
        self._dwell_freq: float | None = None      # dwell clock Hz from last Launch R command
        self._event_accum: list = [0]              # [int] — incremented by data thread, read by GUI
        self._data_streaming = False

        self._build_ui(parent, default_sender_ip, default_cmd_port, default_data_port, default_ssh_user)
        self._schedule_rate_update()


    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self, parent, sender_ip, cmd_port, data_port, ssh_user='user') -> None:
        frame = ttk.LabelFrame(parent, text=f'Node {self.node_id}')
        frame.grid(row=self.node_id - 1, column=0, sticky='ew', padx=10, pady=(6, 2))
        parent.columnconfigure(0, weight=1)

        # Row 0 — sender IP + cmd port
        ttk.Label(frame, text='Sender IP:').grid(row=0, column=0, sticky='w', padx=8, pady=4)
        self.ip_var = tk.StringVar(value=sender_ip)
        self._ip_entry = ttk.Entry(frame, textvariable=self.ip_var, width=16)
        self._ip_entry.grid(row=0, column=1, sticky='w')

        ttk.Label(frame, text='Cmd port:').grid(row=0, column=2, sticky='w', padx=(12, 4))
        self.cmd_port_var = tk.StringVar(value=str(cmd_port))
        self._cmd_port_entry = ttk.Entry(frame, textvariable=self.cmd_port_var, width=7)
        self._cmd_port_entry.grid(row=0, column=3, sticky='w')

        ttk.Label(frame, text='Data port:').grid(row=0, column=4, sticky='w', padx=(12, 4))
        self.data_port_var = tk.StringVar(value=str(data_port))
        self._data_port_entry = ttk.Entry(frame, textvariable=self.data_port_var, width=7)
        self._data_port_entry.grid(row=0, column=5, sticky='w', padx=(0, 8))

        # Row 1 — SSH user + mask file
        ttk.Label(frame, text='SSH user:').grid(row=1, column=0, sticky='w', padx=8, pady=4)
        self.ssh_user_var = tk.StringVar(value=ssh_user)
        self._ssh_user_entry = ttk.Entry(frame, textvariable=self.ssh_user_var, width=14)
        self._ssh_user_entry.grid(row=1, column=1, sticky='w')

        ttk.Label(frame, text='Mask file:').grid(row=1, column=2, sticky='w', padx=(12, 4))
        self.mask_var = tk.StringVar(value='')
        self._mask_entry = ttk.Entry(frame, textvariable=self.mask_var, width=22)
        self._mask_entry.grid(row=1, column=3, columnspan=3, sticky='w', padx=(0, 8))

        # Row 2 — status + launch / connect buttons
        self.ctrl_status_var = tk.StringVar(value='● Disconnected')
        self._ctrl_lbl = tk.Label(frame, textvariable=self.ctrl_status_var,
                                   fg='#cc3333', font=('TkDefaultFont', 9, 'bold'), anchor='w')
        self._ctrl_lbl.grid(row=2, column=0, columnspan=2, sticky='w', padx=8, pady=(2, 2))

        self._launch_btn = ttk.Button(frame, text='Launch', width=9,
                                      command=self._on_launch)
        self._launch_btn.grid(row=2, column=2, columnspan=2, sticky='e', padx=(0, 4), pady=4)

        self._connect_btn = ttk.Button(frame, text='Connect', width=9,
                                       command=self._toggle)
        self._connect_btn.grid(row=2, column=4, columnspan=2, sticky='e', padx=(0, 8), pady=4)

        # Row 3 — data status
        self.data_status_var = tk.StringVar(value='  Data: ● Idle')
        self._data_lbl = tk.Label(frame, textvariable=self.data_status_var,
                                   fg='#888888', font=('TkDefaultFont', 9), anchor='w')
        self._data_lbl.grid(row=3, column=0, columnspan=6, sticky='w', padx=8, pady=(0, 4))

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    def _toggle(self) -> None:
        if self._state == 'idle':
            self._connect()
        else:
            self._disconnect()

    def _connect(self) -> None:
        sender_ip = self.ip_var.get().strip()
        try:
            cmd_port  = int(self.cmd_port_var.get())
            data_port = int(self.data_port_var.get())
        except ValueError:
            self.log_fn(f'Node {self.node_id}: invalid port value.\n')
            return

        # Start data server first (must be listening before we send START)
        try:
            self._data_server = start_server(data_port)
        except OSError as exc:
            self.log_fn(f'Node {self.node_id}: cannot bind data port {data_port} — {exc}\n')
            return

        # Connect control socket to sender's command server
        try:
            ctrl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ctrl.settimeout(5.0)
            ctrl.connect((sender_ip, cmd_port))
            ctrl.settimeout(None)   # back to blocking after connect
            ctrl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            ctrl.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            ctrl.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30_000, 5_000))  # 30 s idle, probe every 5 s
            self._ctrl_sock = ctrl
        except OSError as exc:
            self._data_server.close()
            self._data_server = None
            self.log_fn(f'Node {self.node_id}: cannot connect to {sender_ip}:{cmd_port} — {exc}\n')
            return

        self._set_ctrl_status('ready')
        self.log_fn(f'Node {self.node_id}: connected to {sender_ip}:{cmd_port} '
                    f'(data port {data_port})\n')

        threading.Thread(target=self._read_ctrl_thread, daemon=True).start()
        threading.Thread(target=self._accept_data_thread, daemon=True).start()

    def _disconnect(self) -> None:
        if self._ctrl_sock:
            try:
                self._ctrl_sock.close()
            except Exception:
                pass
            self._ctrl_sock = None
        if self._data_server:
            try:
                self._data_server.close()
            except Exception:
                pass
            self._data_server = None
        if self._data_conn:
            try:
                self._data_conn.close()
            except Exception:
                pass
            self._data_conn = None
        self._gui(lambda: self._set_ctrl_status('idle'))
        self._gui(lambda: self._set_data_status('idle'))
        self.log_fn(f'Node {self.node_id}: disconnected.\n')
        self._trigger_remote_shutdown()

    # ------------------------------------------------------------------
    # Send commands to sender
    # ------------------------------------------------------------------

    def send_start(self, duration: float, test: bool) -> None:
        if self._ctrl_sock is None or self._state == 'idle':
            return
        recv_host  = self._ctrl_sock.getsockname()[0]
        recv_port  = int(self.data_port_var.get())
        output_dir = f'./spad_data/node{self.node_id}'
        self._send_ctrl({
            'cmd':        'start',
            'recv_host':  recv_host,
            'recv_port':  recv_port,
            'output_dir': output_dir,
            'duration':   duration,
            'test':       test,
        })

    def send_abort(self) -> None:
        self._send_ctrl({'cmd': 'abort'})

    def _send_ctrl(self, msg: dict) -> None:
        sock = self._ctrl_sock
        if sock is None:
            return
        data = (json.dumps(msg) + '\n').encode()
        with self._ctrl_lock:
            try:
                sock.sendall(data)
            except OSError:
                pass

    def is_ready(self) -> bool:
        return self._state in ('ready', 'streaming')

    # ------------------------------------------------------------------
    # Background threads
    # ------------------------------------------------------------------

    def _read_ctrl_thread(self) -> None:
        """Read JSON status lines from sender command server."""
        buf = ''
        try:
            while True:
                chunk = self._ctrl_sock.recv(4096)
                if not chunk:
                    break
                buf += chunk.decode('utf-8')
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._on_ctrl_status(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass
        self.log_fn(f'Node {self.node_id}: control connection lost.\n')
        self._gui(lambda: self._set_ctrl_status('idle'))
        self._gui(lambda: self._set_data_status('idle'))
        self._ctrl_sock = None

    def _on_ctrl_status(self, msg: dict) -> None:
        s = msg.get('status')
        if s == 'connecting':
            self.log_fn(f'[N{self.node_id}] Sender connecting to data port …\n')
        elif s == 'streaming':
            self._gui(lambda: self._set_data_status('streaming'))
        elif s == 'done':
            self._gui(lambda: self._set_data_status('idle'))
        elif s == 'log':
            self.log_fn(f'[N{self.node_id}] {msg.get("msg", "")}\n')
        elif s == 'error':
            self.log_fn(f'[N{self.node_id}] Error: {msg.get("msg")}\n')
            self._gui(lambda: self._set_data_status('error'))
        elif s == 'busy':
            self.log_fn(f'[N{self.node_id}] Sender busy — START ignored.\n')

    def _accept_data_thread(self) -> None:
        """Accept data connections from sender and run session loops."""
        while self._data_server is not None:
            try:
                conn, addr = self._data_server.accept()
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._data_conn = conn
            self.log_fn(f'[N{self.node_id}] Data connection from {addr[0]}\n')

            # Clear any stale dwell data from a previous session
            while not self._dwell_q.empty():
                try:
                    self._dwell_q.get_nowait()
                except queue.Empty:
                    break

            hooks = dict(self._get_hooks_fn() if self._get_hooks_fn else {})
            hooks[323] = self._dwell_q  # slave_dwell — needed for clock-offset calibration

            run_session_loop(
                conn,
                log_fn=lambda m: self.log_fn(
                    f'[N{self.node_id}] {m}' if m.endswith('\n') else f'[N{self.node_id}] {m}\n'
                ),
                pixel_hooks=hooks,
                event_accum=self._event_accum,
            )

            self._data_conn = None

    def get_last_dwell_ps(self) -> int | None:
        """Drain the dwell queue and return the last received timestamp, or None."""
        last = None
        while True:
            try:
                raw = self._dwell_q.get_nowait()
                arr = np.frombuffer(raw, dtype=np.int64)
                if len(arr) > 0:
                    last = int(arr[-1])
            except queue.Empty:
                break
        return last

    def get_all_dwell_ps(self) -> np.ndarray:
        """Drain the dwell queue and return all timestamps as a sorted int64 array."""
        chunks = []
        while True:
            try:
                chunks.append(self._dwell_q.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return np.array([], dtype=np.int64)
        return np.concatenate([np.frombuffer(c, dtype=np.int64).copy() for c in chunks])

    # ------------------------------------------------------------------
    # Remote launch via SSH
    # ------------------------------------------------------------------

    def _on_launch(self) -> None:
        """Ask for SSH password (main thread) then start the launch thread."""
        if self._state != 'idle':
            self.log_fn(f'Node {self.node_id}: already connected or launching.\n')
            return
        host     = self.ip_var.get().strip()
        username = self.ssh_user_var.get().strip()
        mask     = self.mask_var.get().strip()
        password = simpledialog.askstring(
            'SSH Password',
            f'Password for {username}@{host}:',
            show='*',
            parent=self.root)
        if password is None:
            return
        threading.Thread(
            target=self._ssh_launch,
            args=(host, username, password, mask),
            daemon=True).start()

    def _ssh_launch(self, host: str, username: str,
                    password: str, mask: str) -> None:
        """Background thread: run full node launch sequence then auto-connect."""
        self._gui(lambda: self._set_ctrl_status('launching'))
        self.log_fn(f'Node {self.node_id}: launching remote node …\n')
        self._ssh_creds = (host, username, password)   # store early so shutdown works on any error

        def _log(msg: str) -> None:
            self.log_fn(f'[N{self.node_id}] {msg}' if msg.endswith('\n')
                        else f'[N{self.node_id}] {msg}\n')

        try:
            self._dwell_freq = ssh_launcher.launch_node(
                host=host, username=username, password=password,
                mask_filename=mask, log_fn=_log)
            time.sleep(3)           # give sender.py command server time to start
            self._gui(self._connect)
        except ssh_launcher.UncommittedChangesError as exc:
            changes = str(exc)
            self._gui(lambda: messagebox.showwarning(
                'Uncommitted Changes',
                f'Node {self.node_id} has uncommitted changes on the sender — '
                f'git pull skipped.\n\n{changes}'))
            self._trigger_remote_shutdown()
            self._gui(lambda: self._set_ctrl_status('idle'))
        except Exception as exc:
            self.log_fn(f'Node {self.node_id}: launch failed — {exc}\n')
            self._trigger_remote_shutdown()
            self._gui(lambda: self._set_ctrl_status('idle'))

    def _trigger_remote_shutdown(self) -> None:
        """If SSH creds are available, start a non-daemon thread to kill lSPAD."""
        if self._ssh_creds:
            creds, self._ssh_creds = self._ssh_creds, None
            self._shutdown_thread = threading.Thread(
                target=self._shutdown_remote, args=creds, daemon=False)
            self._shutdown_thread.start()

    def _shutdown_remote(self, host: str, username: str, password: str) -> None:
        """Background thread: SSH in and kill lSPAD on the sender machine."""
        self.log_fn(f'Node {self.node_id}: shutting down lSPAD on {host} …\n')
        try:
            ssh_launcher.shutdown_lspad(host, username, password)
            self.log_fn(f'Node {self.node_id}: lSPAD shut down.\n')
        except Exception as exc:
            self.log_fn(f'Node {self.node_id}: lSPAD shutdown failed — {exc}\n')

    # ------------------------------------------------------------------
    # Health check  (main thread)
    # ------------------------------------------------------------------

    def health_check(self) -> None:
        if self._ctrl_sock is not None and self._state != 'idle':
            if not check_connection(self._ctrl_sock):
                self.log_fn(f'Node {self.node_id}: health check failed — disconnecting.\n')
                self._disconnect()

    # ------------------------------------------------------------------
    # Status helpers  (main thread only)
    # ------------------------------------------------------------------

    def _set_ctrl_status(self, state: str) -> None:
        self._state = state
        entries = [self._ip_entry, self._cmd_port_entry,
                   self._data_port_entry, self._ssh_user_entry, self._mask_entry]
        if state == 'idle':
            self.ctrl_status_var.set('● Disconnected')
            self._ctrl_lbl.config(fg='#cc3333')
            self._launch_btn.config(state='normal')
            self._connect_btn.config(text='Connect', state='normal')
            for e in entries:
                e.config(state='normal')
        elif state == 'launching':
            self.ctrl_status_var.set('● Launching …')
            self._ctrl_lbl.config(fg='#cc9900')
            self._launch_btn.config(state='disabled')
            self._connect_btn.config(state='disabled')
            for e in entries:
                e.config(state='disabled')
        else:  # 'ready' or 'streaming'
            self.ctrl_status_var.set('● Connected')
            self._ctrl_lbl.config(fg='#33aa33')
            self._launch_btn.config(state='disabled')
            self._connect_btn.config(text='Disconnect', state='normal')
            for e in entries:
                e.config(state='disabled')

    def _set_data_status(self, state: str) -> None:
        self._data_streaming = (state == 'streaming')
        if state == 'streaming':
            self.data_status_var.set('  Data: ● Streaming')
            self._data_lbl.config(fg='#33aa33')
        elif state == 'error':
            self.data_status_var.set('  Data: ● Error')
            self._data_lbl.config(fg='#cc3333')
        else:
            self.data_status_var.set('  Data: ● Idle')
            self._data_lbl.config(fg='#888888')

    def _schedule_rate_update(self) -> None:
        self.root.after(10_000, self._update_rate)

    def _update_rate(self) -> None:
        count = self._event_accum[0]
        self._event_accum[0] = 0
        if self._data_streaming:
            rate = count / 10.0
            if rate >= 1e6:
                rate_str = f'{rate/1e6:.2f} Mcps'
            elif rate >= 1e3:
                rate_str = f'{rate/1e3:.1f} kcps'
            else:
                rate_str = f'{rate:.0f} cps'
            self.data_status_var.set(f'  Data: ● Streaming   {rate_str}')
        self._schedule_rate_update()

    def _gui(self, fn) -> None:
        self.root.after(0, fn)


# ---------------------------------------------------------------------------
# Main receiver GUI
# ---------------------------------------------------------------------------

class ReceiverGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title('SPAD Receiver — Master Controller')
        self.root.resizable(False, False)

        self._log_queue: queue.Queue = queue.Queue()
        self._run_id = 0

        self._correlate_win = CorrelateWindow(root)
        self._monitor_abort: threading.Event | None = None
        self._build_ui()
        self._poll_log()
        self._schedule_health_check()
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        nodes_frame = ttk.Frame(self.root)
        nodes_frame.grid(row=0, column=0, sticky='ew')

        self.node1 = NodePanel(nodes_frame, self.root,
                               node_id=1,
                               default_sender_ip='192.168.1.11',
                               default_cmd_port=50010,
                               default_data_port=50007,
                               default_ssh_user='labcomp1',
                               log_fn=self._enqueue_log,
                               get_hooks_fn=lambda: self._correlate_win.hooks_node1)
        self.node2 = NodePanel(nodes_frame, self.root,
                               node_id=2,
                               default_sender_ip='192.168.1.12',
                               default_cmd_port=50010,
                               default_data_port=50008,
                               default_ssh_user='oreni',
                               log_fn=self._enqueue_log,
                               get_hooks_fn=lambda: self._correlate_win.hooks_node2)

        # ── acquisition controls ───────────────────────────────────────
        acq = ttk.LabelFrame(self.root, text='Acquisition')
        acq.grid(row=1, column=0, padx=10, pady=6, sticky='ew')

        ttk.Label(acq, text='Mode:').grid(row=0, column=0, sticky='w', padx=8, pady=6)
        self.test_var = tk.BooleanVar(value=False)
        ttk.Radiobutton(acq, text='Real', variable=self.test_var,
                        value=False).grid(row=0, column=1, sticky='w')
        ttk.Radiobutton(acq, text='Monitor', variable=self.test_var,
                        value=True).grid(row=0, column=2, sticky='w', padx=(0, 16))

        self._sparse_cal_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(acq, text='Sparse waveform calibration (auto 5 s)',
                        variable=self._sparse_cal_var,
                        ).grid(row=1, column=0, columnspan=5, sticky='w', padx=8, pady=(0, 6))

        ttk.Label(acq, text='Duration (s):').grid(row=0, column=3, sticky='w', padx=(12, 4))
        self.duration_var = tk.StringVar(value='1')
        ttk.Entry(acq, textvariable=self.duration_var, width=8).grid(
            row=0, column=4, sticky='w')

        # Progress bar
        prog_frame = ttk.LabelFrame(self.root, text='Progress')
        prog_frame.grid(row=2, column=0, padx=10, pady=(0, 4), sticky='ew')

        self._progress_var = tk.IntVar(value=0)
        self._progressbar = ttk.Progressbar(prog_frame, variable=self._progress_var,
                        maximum=100, length=480, mode='determinate')
        self._progressbar.grid(row=0, column=0, padx=8, pady=6)
        self._progress_lbl = ttk.Label(prog_frame, text='0 %', width=5, anchor='e')
        self._progress_lbl.grid(row=0, column=1, padx=(0, 8))
        self._timer_lbl = ttk.Label(prog_frame, text='00:00:00', width=10, anchor='w',
                                    font=('Courier', 10))
        self._timer_lbl.grid(row=0, column=0, padx=8, pady=6)
        self._timer_lbl.grid_remove()

        btn_frame = ttk.Frame(acq)
        btn_frame.grid(row=0, column=5, padx=16, pady=6)

        self.start_btn = ttk.Button(btn_frame, text='START ALL', width=12,
                                    command=self._start_all)
        self.start_btn.grid(row=0, column=0, padx=6)

        self.abort_btn = ttk.Button(btn_frame, text='ABORT ALL', width=12,
                                    command=self._abort_all)
        self.abort_btn.grid(row=0, column=1, padx=6)

        # ── log ───────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(self.root, text='Log')
        log_frame.grid(row=3, column=0, padx=10, pady=(0, 10), sticky='nsew')

        self.log = scrolledtext.ScrolledText(
            log_frame, width=72, height=12, state='disabled',
            font=('Courier', 9), background='#1e1e1e', foreground='#d4d4d4',
        )
        self.log.pack(padx=6, pady=6)

    # ------------------------------------------------------------------
    # Acquisition control
    # ------------------------------------------------------------------

    def _start_all(self) -> None:
        try:
            duration = float(self.duration_var.get())
            if duration < 0:
                raise ValueError
        except ValueError:
            self._enqueue_log('Error: duration must be a non-negative number (0 = indefinite).\n')
            return

        if self.test_var.get():
            self._start_monitor(duration)
            return

        sent = 0
        for node in (self.node1, self.node2):
            if node.is_ready():
                node.send_start(duration, False)
                sent += 1

        if sent == 0:
            self._enqueue_log('No nodes connected — nothing started.\n')
            return

        self._run_id += 1
        if duration == 0:
            self._enqueue_log(f'START sent to {sent} node(s) (real, indefinite).\n')
            self._start_timer()
        else:
            self._enqueue_log(f'START sent to {sent} node(s) (real, {duration} s).\n')
            self._show_progress_bar()
            self._set_progress(0)
            step_ms = max(1, int(duration / 10 * 1000))
            self._schedule_progress(step_ms, 1, self._run_id)

        if self._correlate_win.is_enabled:
            if self._sparse_cal_var.get():
                self._enqueue_log('Sparse cal: collecting dwell for 5 s…\n')
                self.root.after(5000, self._apply_sparse_dwell_offset)
            else:
                self._show_dwell_popup()

    def _abort_all(self) -> None:
        if self._monitor_abort is not None:
            self._monitor_abort.set()
        for node in (self.node1, self.node2):
            if node.is_ready():
                node.send_abort()
        self._run_id += 1
        self._show_progress_bar()
        self._set_progress(0)
        self._enqueue_log('ABORT sent to all connected nodes.\n')

    # ------------------------------------------------------------------
    # Monitor mode  (environmental polling via SSH R command)
    # ------------------------------------------------------------------

    def _start_monitor(self, duration: float) -> None:
        if self._monitor_abort is not None and not self._monitor_abort.is_set():
            self._enqueue_log('Monitor already running — click ABORT to stop it first.\n')
            return

        # Collect SSH credentials for each node; ask for password if not cached.
        node_creds: list[tuple[int, tuple]] = []
        for node in (self.node1, self.node2):
            creds = node._ssh_creds
            if creds is None:
                host = node.ip_var.get().strip()
                user = node.ssh_user_var.get().strip()
                if not host:
                    continue
                pw = simpledialog.askstring(
                    'SSH Password',
                    f'Password for {user}@{host} (Node {node.node_id}):',
                    show='*', parent=self.root)
                if pw:
                    creds = (host, user, pw)
                else:
                    self._enqueue_log(f'Node {node.node_id}: skipped (no password).\n')
                    continue
            node_creds.append((node.node_id, creds))

        if not node_creds:
            self._enqueue_log('No nodes available for monitoring.\n')
            return

        self._monitor_abort = threading.Event()
        self._enqueue_log(
            f'Monitor started: {duration:.0f} s, {len(node_creds)} node(s), '
            f'R poll every 10 s.\n')

        for node_id, creds in node_creds:
            threading.Thread(
                target=self._run_monitor_node,
                args=(node_id, creds, duration),
                daemon=True).start()

        self._run_id += 1
        self._set_progress(0)
        step_ms = max(1, int(duration / 10 * 1000))
        self._schedule_progress(step_ms, 1, self._run_id)

    def _run_monitor_node(self, node_id: int, creds: tuple, duration: float) -> None:
        host, username, password = creds
        rows: list[dict] = []

        def log(msg: str) -> None:
            self._enqueue_log(
                f'[N{node_id}] {msg}' if msg.endswith('\n') else f'[N{node_id}] {msg}\n')

        try:
            ssh_launcher.ensure_lspad_running(host, username, password, log)
        except Exception as exc:
            log(f'Cannot start lSPAD: {exc}')
            return

        log('Environmental monitoring started.')
        start_time = time.time()

        while not self._monitor_abort.is_set():
            elapsed = time.time() - start_time
            if elapsed >= duration:
                break

            ts = time.strftime('%Y-%m-%dT%H:%M:%S')
            reading = ssh_launcher.query_r(host, username, password)
            if reading is not None:
                reading['timestamp'] = ts
                reading['elapsed_s'] = round(elapsed, 1)
                rows.append(reading)
                log(
                    f't={elapsed:.0f}s  '
                    f'FPGA={reading["fpga_master_temp_c"]:.1f}°C  '
                    f'PCB={reading["pcb_temp_c"]:.1f}°C  '
                    f'H={reading["humidity_pct"]:.1f}%  '
                    f'Dwell={reading["dwell_freq_hz"]:.3e} Hz')
            else:
                log(f't={elapsed:.0f}s  R command failed.')

            self._monitor_abort.wait(10.0)

        log('Monitoring done.')
        self._save_monitor_csv(node_id, rows)

    def _save_monitor_csv(self, node_id: int, rows: list[dict]) -> None:
        if not rows:
            self._enqueue_log(f'Node {node_id}: no monitor data collected.\n')
            return

        os.makedirs('spad_data', exist_ok=True)
        ts = time.strftime('%Y%m%d_%H%M%S')
        path = os.path.join('spad_data', f'monitor_node{node_id}_{ts}.csv')

        cols = [
            'timestamp', 'elapsed_s',
            'fpga_master_temp_c', 'fpga_slave_temp_c',
            'pcb_temp_c', 'pcb_temp2_c', 'chip_pcb_temp_c',
            'humidity_pct',
            'laser_freq_hz', 'frame_freq_hz', 'line_freq_hz', 'dwell_freq_hz',
        ]
        try:
            with open(path, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=cols)
                writer.writeheader()
                writer.writerows(rows)
            self._enqueue_log(
                f'Node {node_id}: {len(rows)} readings saved → {path}\n')
        except Exception as exc:
            self._enqueue_log(f'Node {node_id}: failed to save CSV — {exc}\n')

    def _set_progress(self, pct: int) -> None:
        self._progress_var.set(pct)
        self._progress_lbl.config(text=f'{pct} %')

    def _schedule_progress(self, step_ms: int, step: int, run_id: int) -> None:
        def tick() -> None:
            if run_id != self._run_id:
                return
            self._set_progress(step * 10)
            if step < 10:
                self._schedule_progress(step_ms, step + 1, run_id)
        self.root.after(step_ms, tick)

    def _start_timer(self) -> None:
        self._progressbar.grid_remove()
        self._progress_lbl.grid_remove()
        self._timer_lbl.config(text='00:00:00')
        self._timer_lbl.grid()
        self._schedule_timer(0, self._run_id)

    def _show_progress_bar(self) -> None:
        self._timer_lbl.grid_remove()
        self._progressbar.grid()
        self._progress_lbl.grid()

    def _schedule_timer(self, elapsed_s: int, run_id: int) -> None:
        def tick() -> None:
            if run_id != self._run_id:
                return
            new_elapsed = elapsed_s + 10
            h, rem = divmod(new_elapsed, 3600)
            m, s = divmod(rem, 60)
            self._timer_lbl.config(text=f'{h:02d}:{m:02d}:{s:02d}')
            self._schedule_timer(new_elapsed, run_id)
        self.root.after(10_000, tick)

    # ------------------------------------------------------------------
    # Dwell calibration popup
    # ------------------------------------------------------------------

    def _show_dwell_popup(self) -> None:
        popup = tk.Toplevel(self.root)
        popup.title('Dwell Calibration')
        popup.resizable(False, False)
        popup.grab_set()
        popup.protocol('WM_DELETE_WINDOW', lambda: None)  # block accidental close

        ttk.Label(popup,
                  text='Press the DWELL button on the detector,\nthen click OK.',
                  justify='center',
                  font=('TkDefaultFont', 11)).pack(padx=30, pady=(20, 8))

        err_var = tk.StringVar(value='')
        ttk.Label(popup, textvariable=err_var,
                  foreground='#cc3333', wraplength=300).pack(padx=20, pady=(0, 4))

        btn_frame = ttk.Frame(popup)
        btn_frame.pack(pady=(4, 20))

        def on_ok():
            if self._apply_dwell_offset(err_var) is None:
                popup.destroy()

        ttk.Button(btn_frame, text='OK', width=10,
                   command=on_ok).grid(row=0, column=0, padx=6)
        ttk.Button(btn_frame, text='Skip (offset = 0)', width=16,
                   command=lambda: [
                       self._correlate_win.start_with_offset(0),
                       self._enqueue_log('Dwell skipped — offset set to 0.\n'),
                       popup.destroy(),
                   ]).grid(row=0, column=1, padx=6)

        popup.transient(self.root)
        popup.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width()  - popup.winfo_width())  // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - popup.winfo_height()) // 2
        popup.geometry(f'+{x}+{y}')

    def _apply_sparse_dwell_offset(self) -> None:
        """Autonomous sparse-waveform dwell calibration using the first 5 s of dwell data."""
        t1 = self.node1.get_all_dwell_ps()
        t2 = self.node2.get_all_dwell_ps()
        MIN_EVENTS = 5
        if t1.size < MIN_EVENTS or t2.size < MIN_EVENTS:
            self._enqueue_log(
                f'Sparse cal failed: {t1.size} / {t2.size} dwell events '
                f'(need ≥{MIN_EVENTS} each). Setting offset = 0.\n'
            )
            self._correlate_win.start_with_offset(0)
            return
        offset_ps, details = estimate_offset(
            t1, t2,
            cluster_tol=100_000,   # 100 ns in ps — covers jitter, << inter-pulse gap
            return_details=True,
        )
        offset = int(round(offset_ps))
        self._enqueue_log(
            f'Sparse cal: offset = {offset:+,} ps  '
            f'({details["n_matched"]} matched pairs, '
            f'SEM = {details["sem"]:.0f} ps, '
            f'streams: {details["n1"]} / {details["n2"]} events)\n'
        )
        self._correlate_win.start_with_offset(offset)

    def _apply_dwell_offset(self, err_var: tk.StringVar) -> str | None:
        """Read dwell queues from both nodes, compute offset, pass to correlator."""
        last1 = self.node1.get_last_dwell_ps()
        last2 = self.node2.get_last_dwell_ps()

        if last1 is None:
            msg = 'No dwell signal received on Node 1 yet — press DWELL and retry.'
            err_var.set(msg)
            return msg
        if last2 is None:
            msg = 'No dwell signal received on Node 2 yet — press DWELL and retry.'
            err_var.set(msg)
            return msg

        offset = last2 - last1
        self._enqueue_log(f'Dwell offset: {offset:+,} ps  '
                          f'(node1={last1:,} ps, node2={last2:,} ps)\n')
        self._correlate_win.start_with_offset(offset)
        return None

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def _schedule_health_check(self) -> None:
        self.root.after(HEALTH_CHECK_MS, self._health_check)

    def _health_check(self) -> None:
        self.node1.health_check()
        self.node2.health_check()
        self._schedule_health_check()

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        """Disconnect all nodes (triggers lSPAD shutdown), wait, then exit."""
        for node in (self.node1, self.node2):
            if node._state != 'idle':
                node._disconnect()
        for node in (self.node1, self.node2):
            if node._shutdown_thread and node._shutdown_thread.is_alive():
                node._shutdown_thread.join(timeout=8)
        self.root.destroy()

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------

    def _enqueue_log(self, text: str) -> None:
        self._log_queue.put(text)

    def _poll_log(self) -> None:
        try:
            while True:
                text = self._log_queue.get_nowait()
                self.log.config(state='normal')
                self.log.insert(tk.END, text)
                self.log.see(tk.END)
                self.log.config(state='disabled')
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    root = tk.Tk()
    ReceiverGUI(root)
    root.mainloop()
