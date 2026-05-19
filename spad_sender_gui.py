#!/usr/bin/env python
"""
SPAD Sender status display.
All acquisition control comes from the receiver GUI via the command server.
"""

import os
import sys
import queue
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spad_sender import run_command_server, DEFAULT_CMD_PORT


class SpadSenderGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title('SPAD Sender')
        self.root.resizable(False, False)

        self._event_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._poll_events()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── command server config ─────────────────────────────────────
        cfg = ttk.LabelFrame(self.root, text='Command Server')
        cfg.grid(row=0, column=0, padx=10, pady=8, sticky='ew')

        ttk.Label(cfg, text='Port:').grid(row=0, column=0, padx=8, pady=6, sticky='w')
        self.port_var = tk.StringVar(value=str(DEFAULT_CMD_PORT))
        self._port_entry = ttk.Entry(cfg, textvariable=self.port_var, width=8)
        self._port_entry.grid(row=0, column=1, sticky='w')

        self._listen_btn = ttk.Button(cfg, text='Start Listening', width=14,
                                      command=self._start_server)
        self._listen_btn.grid(row=0, column=2, padx=10, pady=6)

        # ── status indicators ─────────────────────────────────────────
        status = ttk.LabelFrame(self.root, text='Status')
        status.grid(row=1, column=0, padx=10, pady=(0, 6), sticky='ew')

        ttk.Label(status, text='Control:').grid(row=0, column=0, sticky='w', padx=8, pady=4)
        self.ctrl_var = tk.StringVar(value='● Not started')
        self._ctrl_lbl = tk.Label(status, textvariable=self.ctrl_var,
                                   fg='#888888', font=('TkDefaultFont', 9, 'bold'), anchor='w')
        self._ctrl_lbl.grid(row=0, column=1, sticky='w', padx=(0, 16))

        ttk.Label(status, text='Data:').grid(row=1, column=0, sticky='w', padx=8, pady=4)
        self.data_var = tk.StringVar(value='● Idle')
        self._data_lbl = tk.Label(status, textvariable=self.data_var,
                                   fg='#888888', font=('TkDefaultFont', 9, 'bold'), anchor='w')
        self._data_lbl.grid(row=1, column=1, sticky='w')

        # ── log ───────────────────────────────────────────────────────
        self.log = scrolledtext.ScrolledText(
            self.root, width=52, height=10, state='disabled',
            font=('Courier', 9), background='#1e1e1e', foreground='#d4d4d4',
        )
        self.log.grid(row=2, column=0, padx=10, pady=(0, 10), sticky='nsew')

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------

    def _start_server(self) -> None:
        try:
            port = int(self.port_var.get())
        except ValueError:
            self._log('Invalid port.\n')
            return

        self._listen_btn.config(state='disabled')
        self._port_entry.config(state='disabled')

        threading.Thread(
            target=run_command_server,
            args=(port, self._event_queue.put),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # Event polling
    # ------------------------------------------------------------------

    def _poll_events(self) -> None:
        try:
            while True:
                evt = self._event_queue.get_nowait()
                self._handle_event(evt)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle_event(self, evt: dict) -> None:
        e = evt.get('event')
        if e == 'listening':
            self.ctrl_var.set(f'● Listening on :{evt["port"]}')
            self._ctrl_lbl.config(fg='#cc9900')
            self._log(f'Listening on port {evt["port"]}\n')
        elif e == 'ctrl_connected':
            self.ctrl_var.set(f'● Controller connected  ({evt["addr"]})')
            self._ctrl_lbl.config(fg='#33aa33')
            self._log(f'Controller connected from {evt["addr"]}\n')
        elif e == 'ctrl_disconnected':
            self.ctrl_var.set('● Listening (controller disconnected)')
            self._ctrl_lbl.config(fg='#cc9900')
            self._log('Controller disconnected.\n')
        elif e == 'streaming':
            self.data_var.set('● Streaming')
            self._data_lbl.config(fg='#33aa33')
        elif e == 'idle':
            self.data_var.set('● Idle')
            self._data_lbl.config(fg='#888888')
        elif e == 'error':
            self.data_var.set('● Error')
            self._data_lbl.config(fg='#cc3333')
            self._log(f'Error: {evt.get("msg")}\n')

    def _log(self, text: str) -> None:
        self.log.config(state='normal')
        self.log.insert(tk.END, text)
        self.log.see(tk.END)
        self.log.config(state='disabled')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    root = tk.Tk()
    SpadSenderGUI(root)
    root.mainloop()
