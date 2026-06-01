#!/usr/bin/env python
"""
SPAD Sender status display.
Starts the command server automatically on launch.
"""

import os
import sys
import queue
import threading
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sender_backend import run_command_server, DEFAULT_CMD_PORT


class SpadSenderGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f'SPAD Sender  —  port {DEFAULT_CMD_PORT}')
        self.root.resizable(False, False)

        self._event_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._poll_events()
        self._start_server()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=20)
        frame.grid()

        self.ctrl_var = tk.StringVar(value='● Listening')
        self._ctrl_lbl = tk.Label(frame, textvariable=self.ctrl_var,
                                   fg='#cc9900', font=('TkDefaultFont', 12, 'bold'), anchor='w')
        self._ctrl_lbl.grid(row=0, column=0, sticky='w', pady=(0, 10))

        self.data_var = tk.StringVar(value='● Standing by')
        self._data_lbl = tk.Label(frame, textvariable=self.data_var,
                                   fg='#888888', font=('TkDefaultFont', 12, 'bold'), anchor='w')
        self._data_lbl.grid(row=1, column=0, sticky='w')

    def _start_server(self) -> None:
        threading.Thread(
            target=run_command_server,
            args=(DEFAULT_CMD_PORT, self._event_queue.put),
            daemon=True,
        ).start()

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
            self.ctrl_var.set('● Listening')
            self._ctrl_lbl.config(fg='#cc9900')
        elif e == 'ctrl_connected':
            self.ctrl_var.set(f'● Connected  ({evt["addr"]})')
            self._ctrl_lbl.config(fg='#33aa33')
        elif e == 'ctrl_disconnected':
            self.ctrl_var.set('● Listening')
            self._ctrl_lbl.config(fg='#cc9900')
        elif e == 'streaming':
            self.data_var.set('● Acquiring')
            self._data_lbl.config(fg='#33aa33')
        elif e == 'idle':
            self.data_var.set('● Standing by')
            self._data_lbl.config(fg='#888888')
        elif e == 'error':
            self.data_var.set('● Error')
            self._data_lbl.config(fg='#cc3333')


if __name__ == '__main__':
    root = tk.Tk()
    SpadSenderGUI(root)
    root.mainloop()
