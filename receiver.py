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
SPARSE_CAL_DURATION_S = 4.194304  # RIGOL sparse-pulse RAF waveform: 2**23 pts @ 2 MSa/s


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
                 get_hooks_fn=None,
                 set_correlate_pixel_fn=None) -> None:
        self.root          = root
        self.node_id       = node_id
        self.log_fn        = log_fn
        self._get_hooks_fn = get_hooks_fn
        self._set_correlate_pixel_fn = set_correlate_pixel_fn

        self._ctrl_sock:   socket.socket | None = None
        self._data_server: socket.socket | None = None
        self._data_conn:   socket.socket | None = None
        self._ctrl_lock    = threading.Lock()
        self._state        = 'idle'   # 'idle' | 'ready' | 'streaming'
        self._dwell_q: queue.Queue = queue.Queue()         # slave_dwell (key 323)
        self._master_dwell_q: queue.Queue = queue.Queue()  # master_dwell (key 320)
        self._output_dir: str | None = None
        self._flush_active = False   # periodic post-calibration dwell-to-disk flush
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
        self._mask_entry = ttk.Entry(frame, textvariable=self.mask_var, width=16)
        self._mask_entry.grid(row=1, column=3, columnspan=2, sticky='w')

        ttk.Label(frame, text='Pixel:').grid(row=1, column=5, sticky='w', padx=(8, 4))
        self.pixel_var = tk.StringVar(value='')
        self.pixel_var.trace_add('write', self._on_pixel_changed)
        self._pixel_entry = ttk.Entry(frame, textvariable=self.pixel_var, width=5)
        self._pixel_entry.grid(row=1, column=6, sticky='w', padx=(0, 8))

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
        self.stop_dwell_flush()
        self.dump_dwell_bins(self.get_all_dwell_ps(), self.get_all_master_dwell_ps())
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
        self._output_dir = output_dir
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
            self.stop_dwell_flush()
            self.dump_dwell_bins(self.get_all_dwell_ps(), self.get_all_master_dwell_ps())
        elif s == 'log':
            self.log_fn(f'[N{self.node_id}] {msg.get("msg", "")}\n')
        elif s == 'error':
            self.log_fn(f'[N{self.node_id}] Error: {msg.get("msg")}\n')
            self._gui(lambda: self._set_data_status('error'))
            self.stop_dwell_flush()
            self.dump_dwell_bins(self.get_all_dwell_ps(), self.get_all_master_dwell_ps())
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

            # Stop any leftover flush loop and clear stale dwell data from a previous session
            self.stop_dwell_flush()
            for q in (self._dwell_q, self._master_dwell_q):
                while not q.empty():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break

            hooks = dict(self._get_hooks_fn() if self._get_hooks_fn else {})
            hooks[320] = self._master_dwell_q  # master_dwell — offset diagnostics
            hooks[323] = self._dwell_q         # slave_dwell — needed for clock-offset calibration

            run_session_loop(
                conn,
                log_fn=lambda m: self.log_fn(
                    f'[N{self.node_id}] {m}' if m.endswith('\n') else f'[N{self.node_id}] {m}\n'
                ),
                pixel_hooks=hooks,
                event_accum=self._event_accum,
            )

            self._data_conn = None

    @staticmethod
    def _drain(q: queue.Queue) -> np.ndarray:
        """Drain a queue of raw int64-timestamp byte chunks into one array."""
        chunks = []
        while True:
            try:
                chunks.append(q.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return np.array([], dtype=np.int64)
        return np.concatenate([np.frombuffer(c, dtype=np.int64).copy() for c in chunks])

    def get_all_dwell_ps(self) -> np.ndarray:
        """Drain the slave_dwell queue and return all timestamps as an int64 array."""
        return self._drain(self._dwell_q)

    def get_all_master_dwell_ps(self) -> np.ndarray:
        """Drain the master_dwell queue and return all timestamps as an int64 array."""
        return self._drain(self._master_dwell_q)

    def dump_dwell_bins(self, slave_arr: np.ndarray, master_arr: np.ndarray) -> None:
        """Append newly drained dwell arrays to this node's session dwell .bin
        files (run_session_loop truncates them to empty at session start, then
        routes keys 320/323 into queues instead of writing them directly, so
        every drain — calibration or periodic flush — must append, not overwrite)."""
        if not self._output_dir or (slave_arr.size == 0 and master_arr.size == 0):
            return
        os.makedirs(self._output_dir, exist_ok=True)
        if slave_arr.size:
            with open(os.path.join(self._output_dir, 'slave_dwell.bin'), 'ab') as f:
                f.write(slave_arr.tobytes())
        if master_arr.size:
            with open(os.path.join(self._output_dir, 'master_dwell.bin'), 'ab') as f:
                f.write(master_arr.tobytes())

    def start_dwell_flush(self, interval_ms: int = 2000) -> None:
        """Begin periodically appending newly arrived dwell events to disk.
        Call once calibration has consumed what it needs from the queues —
        starting earlier would race calibration for the same events."""
        if self._flush_active:
            return
        self._flush_active = True
        self._schedule_dwell_flush(interval_ms)

    def stop_dwell_flush(self) -> None:
        self._flush_active = False

    def _schedule_dwell_flush(self, interval_ms: int) -> None:
        if not self._flush_active:
            return
        self.dump_dwell_bins(self.get_all_dwell_ps(), self.get_all_master_dwell_ps())
        self.root.after(interval_ms, lambda: self._schedule_dwell_flush(interval_ms))

    # ------------------------------------------------------------------
    # Remote launch via SSH
    # ------------------------------------------------------------------

    def _on_pixel_changed(self, *_args) -> None:
        """Mirror the chosen pixel into the mask-file field, purely for display."""
        pixel_str = self.pixel_var.get().strip()
        if pixel_str:
            self.mask_var.set(f'mask_{pixel_str}.txt')

    def _on_launch(self) -> None:
        """Ask for SSH password (main thread) then start the launch thread."""
        if self._state != 'idle':
            self.log_fn(f'Node {self.node_id}: already connected or launching.\n')
            return
        host       = self.ip_var.get().strip()
        username   = self.ssh_user_var.get().strip()
        mask       = self.mask_var.get().strip()
        pixel_str  = self.pixel_var.get().strip()
        mask_pixel = None
        if pixel_str:
            try:
                mask_pixel = int(pixel_str)
            except ValueError:
                self.log_fn(f'Node {self.node_id}: invalid pixel value {pixel_str!r}.\n')
                return
            if not (0 <= mask_pixel <= 319):
                self.log_fn(f'Node {self.node_id}: pixel must be between 0 and 319.\n')
                return
            if self._set_correlate_pixel_fn:
                self._set_correlate_pixel_fn(mask_pixel)
        password = simpledialog.askstring(
            'SSH Password',
            f'Password for {username}@{host}:',
            show='*',
            parent=self.root)
        if password is None:
            return
        threading.Thread(
            target=self._ssh_launch,
            args=(host, username, password, mask, mask_pixel),
            daemon=True).start()

    def _ssh_launch(self, host: str, username: str,
                    password: str, mask: str, mask_pixel: int | None) -> None:
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
                mask_pixel=mask_pixel,
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
                               get_hooks_fn=lambda: self._correlate_win.hooks_node1,
                               set_correlate_pixel_fn=lambda pix: self._correlate_win.px1_var.set(str(pix)))
        self.node2 = NodePanel(nodes_frame, self.root,
                               node_id=2,
                               default_sender_ip='192.168.1.12',
                               default_cmd_port=50010,
                               default_data_port=50008,
                               default_ssh_user='oreni',
                               log_fn=self._enqueue_log,
                               get_hooks_fn=lambda: self._correlate_win.hooks_node2,
                               set_correlate_pixel_fn=lambda pix: self._correlate_win.px2_var.set(str(pix)))

        # ── acquisition controls ───────────────────────────────────────
        acq = ttk.LabelFrame(self.root, text='Acquisition')
        acq.grid(row=1, column=0, padx=10, pady=6, sticky='ew')

        ttk.Label(acq, text='Mode:').grid(row=0, column=0, sticky='w', padx=8, pady=6)
        self.test_var = tk.BooleanVar(value=False)
        ttk.Radiobutton(acq, text='Real', variable=self.test_var,
                        value=False).grid(row=0, column=1, sticky='w')
        ttk.Radiobutton(acq, text='Monitor', variable=self.test_var,
                        value=True).grid(row=0, column=2, sticky='w', padx=(0, 16))

        ttk.Label(acq, text=f'Sparse waveform calibration (auto {SPARSE_CAL_DURATION_S:.2f} s)',
                  ).grid(row=1, column=0, columnspan=5, sticky='w', padx=8, pady=(0, 6))

        self._cal_status_var = tk.StringVar(value='')
        self._cal_status_lbl = tk.Label(acq, textvariable=self._cal_status_var, anchor='w')
        self._cal_status_lbl.grid(row=2, column=0, columnspan=5, sticky='w', padx=8, pady=(0, 6))

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
        self._set_cal_status('')
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
            self._enqueue_log(
                f'Sparse cal: collecting dwell for {SPARSE_CAL_DURATION_S:.2f} s…\n')
            self._set_cal_status(
                f'● Calibrating dwell offset ({SPARSE_CAL_DURATION_S:.2f} s)…',
                color='#cc8800')
            self.root.after(round(SPARSE_CAL_DURATION_S * 1000),
                             self._apply_sparse_dwell_offset)

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
    # Dwell calibration
    # ------------------------------------------------------------------

    def _set_cal_status(self, text: str, color: str = 'black') -> None:
        self._cal_status_var.set(text)
        self._cal_status_lbl.config(fg=color)

    def _apply_sparse_dwell_offset(self) -> None:
        """Autonomous sparse-waveform dwell calibration using the first
        SPARSE_CAL_DURATION_S of dwell data."""
        t1 = self.node1.get_all_dwell_ps()          # slave_dwell
        t2 = self.node2.get_all_dwell_ps()          # slave_dwell
        m1 = self.node1.get_all_master_dwell_ps()   # master_dwell
        m2 = self.node2.get_all_master_dwell_ps()   # master_dwell

        # Dwell keys are intercepted into queues instead of being written to
        # disk by run_session_loop — dump them to the usual dwell .bin files now.
        self.node1.dump_dwell_bins(t1, m1)
        self.node2.dump_dwell_bins(t2, m2)

        MIN_EVENTS = 5
        if t1.size < MIN_EVENTS or t2.size < MIN_EVENTS:
            self._enqueue_log(
                f'Sparse cal failed: {t1.size} / {t2.size} slave dwell events '
                f'(need ≥{MIN_EVENTS} each). Setting offset = 0.\n'
            )
            self._set_cal_status(
                f'● Calibration failed ({t1.size}/{t2.size} events) — offset = 0',
                color='#cc3333')
            self._correlate_win.start_with_offset(0)
            self.node1.start_dwell_flush()
            self.node2.start_dwell_flush()
            return

        cluster_tol = 10_000  # 10 ns: excludes ±32 ns TDC doublet sidelobes
        slave_offset_ps, slave_details = estimate_offset(
            t1, t2, cluster_tol=cluster_tol, return_details=True)

        if m1.size >= MIN_EVENTS and m2.size >= MIN_EVENTS:
            master_offset_ps, master_details = estimate_offset(
                m1, m2, cluster_tol=cluster_tol, return_details=True)
        else:
            master_offset_ps, master_details = float('nan'), None

        slave_offset = int(round(slave_offset_ps))

        self._enqueue_log('Automatic dwell calibration\n')
        if master_details is not None and not np.isnan(master_offset_ps):
            self._enqueue_log(
                f'  Master offset = {int(round(master_offset_ps)):+,} ps  '
                f'({master_details["n_matched"]} matched pairs, '
                f'SEM = {master_details["sem"]:.0f} ps, '
                f'streams: {master_details["n1"]} / {master_details["n2"]} events)\n'
            )
        else:
            self._enqueue_log(
                f'  Master offset: unavailable ({m1.size} / {m2.size} master dwell events)\n'
            )
        self._enqueue_log(
            f'  Slave offset  = {slave_offset:+,} ps  '
            f'({slave_details["n_matched"]} matched pairs, '
            f'SEM = {slave_details["sem"]:.0f} ps, '
            f'streams: {slave_details["n1"]} / {slave_details["n2"]} events)\n'
        )
        self._enqueue_log('Acquiring\n')

        self._set_cal_status(
            f'● Calibrated — offset {slave_offset:+,} ps, acquisition running',
            color='#228822')
        self._correlate_win.start_with_offset(slave_offset)
        self.node1.start_dwell_flush()
        self.node2.start_dwell_flush()

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
