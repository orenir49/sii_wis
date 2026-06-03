# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Activate virtual environment (Windows)
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run master receiver GUI (on master PC)
python receiver.py

# Run sender GUI (on each SPAD detector PC)
python sender.py

# Standalone single-node data receiver
python receiver_backend.py [--port 50007]

# One-shot sender node setup (run as Administrator on sender PC)
.\setup_node.ps1

# Offline analysis
jupyter notebook spad_new.ipynb
```

## Architecture

This is a **SPAD (Single Photon Avalanche Diode) multi-node acquisition system** for two-detector quantum optics experiments. It runs across a small LAN: one master PC (receiver) controls two sender PCs (each connected to a SPAD detector).

### Two-role design

**Receiver (master PC)** — `receiver.py`  
Controls up to 2 sender nodes. Each node gets a `NodePanel` instance that manages both channels:
- **Control channel**: receiver → sender, JSON commands over TCP (connect, start, stop, shutdown)
- **Data channel**: sender → receiver, binary timestamp stream over TCP

**Sender (detector PC)** — `sender.py` + `sender_backend.py`  
Minimal GUI that starts a command server thread on launch. Receives JSON commands from the receiver, talks to the local `lSPAD.exe` hardware driver over TCP (port 9999), and streams timestamped pixel data back to the receiver.

### Key files

| File | Role |
|---|---|
| `receiver.py` | Master GUI; `NodePanel` per sender node |
| `receiver_backend.py` | TCP data server: `start_server()`, `run_session_loop()`, `check_connection()` |
| `sender.py` | Sender GUI shell; starts command server thread |
| `sender_backend.py` | Command server + lSPAD TCP client; contains `PIXMAP` (320-pixel array mapping) |
| `correlate.py` | `CorrelateWindow` — live g² histogram with Numba JIT kernel |
| `ssh_launcher.py` | Paramiko-based remote automation for launching sender nodes |
| `setup_node.ps1` | One-shot sender node setup: OpenSSH, firewall, git clone, venv |
| `spad_new.ipynb` | Offline g² analysis notebook |

### Wire protocol

Frames: 8-byte header `(key_id: uint32 big-endian, n_bytes: uint32 big-endian)` followed by payload.

- `key_id` 0–319: pixel timestamp data (`px_000.bin` … `px_319.bin`)
- `key_id` 320–325: sync signals — master/slave dwell, line, frame
- `0xFFFFFFFF` (KEY_SETUP): payload is UTF-8 output directory path — opens one acquisition session
- `0xFFFFFFFE` (KEY_END): empty payload — closes the session; `run_session_loop()` loops back for the next

Pixel mapping: `PIXMAP` in `sender_backend.py` maps lSPAD pixel indices to output keys. Slave pixels occupy indices 0–169, master pixels 170–319.

### Live g² correlator

`correlate.py` integrates with `run_session_loop()` via `pixel_hooks: dict[key_id, queue.Queue]`. Matching chunks are enqueued instead of written to disk. `CorrelateWindow` accumulates int64 timestamps from two pixel queues and calls the Numba JIT `_multistart_multistop()` kernel in a background thread. The kernel is pre-warmed at startup to avoid the first-call JIT delay.

### SSH remote launch (`ssh_launcher.py`)

Automates sender node startup via paramiko password auth:
1. Find and start `lSPAD.exe` detached via WMI `Win32_Process.Create` (survives SSH disconnect)
2. Wait for lSPAD port 9999 to open
3. Apply pixel mask (`M,<path>`), run TDC calibration (`T,v,1` → `T,c,1`)
4. Git pull the repo, launch `sender.py` detached via venv `pythonw.exe`

### Data files

Stored under `spad_data/` (gitignored). Each acquisition session creates a subdirectory containing `px_000.bin` … `px_319.bin` (raw int64 timestamps per pixel) and `master_dwell.bin`, `slave_dwell.bin`, `master_line.bin`, `slave_line.bin`, `master_frame.bin`, `slave_frame.bin` (synchronization signals).
