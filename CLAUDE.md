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

# Arc-line spectral alignment between two detectors
python .claude/skills/spectral-align/align_arc.py REF.txt OTHER.txt --outdir figs

# ...and emit the pixel-pair list that fit implies, for the multi-pair correlator
python .claude/skills/spectral-align/align_arc.py REF.txt OTHER.txt --emit-pairs 120 200

# Preview a pair list without running a fit (identity / affine / grid / file)
python tools\pair_map.py --mode affine --lo 120 --hi 200 -a 1.037 -b -2.4
python tools\pair_map.py --selftest

# SII bunching-excess / integration-time calculator
python tools\sii_calculator.py

# Peak-annotated g2 histogram + count-distribution figures from a saved correlate.py result
python tools\plot_g2_result.py spad_data\147_147_resolve_peak.txt --outdir figs\<DD-M-YY>

# Offline g2 for one or more pixel pairs, robust offset (matches the live correlator) -- run
# interactively on real hardware, not in a sandboxed/CI shell (see the script's own docstring)
python tools\analyze_g2_pairs_offline.py --base spad_data\<dir> 147x147 147x168 168x147 168x168
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
| `sender_backend.py` | Command server + lSPAD TCP client; contains `PIXMAP` (320-pixel array mapping); logs abnormal marker ids live (see below) |
| `correlate.py` | `CorrelateWindow` (single-pair) + `QuadCorrelateWindow` (2 pixels/node, 4 pairwise g² histograms — e.g. mask_two.txt) live correlators, Numba JIT kernel; "Compute R…" button opens `tools/sii_calculator.py` |
| `tools/sii_calculator_backend.py` | Pure formulas: `<\|V\|^2>`, coherence time, bunching-excess `R`, required integration time |
| `tools/sii_calculator.py` | `SIICalculatorWindow` — interactive bunching-excess / integration-time calculator |
| `tools/plot_g2_result.py` | Peak-annotated g² histogram + count-distribution PNGs from a saved `{px1}_{px2}_{suffix}.txt` |
| `tools/analyze_g2_pairs_offline.py` | Offline g² for arbitrary pixel pairs with the robust slave-dwell clock offset (matches the live correlator) |
| `tools/pair_map.py` | Pure (node-1, node-2) pair derivation for the multi-pair correlator — identity / affine / grid / file modes, mask cross-check, `--selftest` |
| `ssh_launcher.py` | Paramiko-based remote automation for launching sender nodes |
| `setup_node.ps1` | One-shot sender node setup: OpenSSH, firewall, git clone, venv |
| `spad_new.ipynb` | Offline g² analysis notebook |
| `LSPAD_CLI.md` | Reference for `lSPAD.exe`'s own TCP command protocol (port 9999) |
| `.claude/skills/spectral-align/align_arc.py` | Arc-line peak matching + affine pixel-mapping fit between two SPAD spectra |

### Wire protocol

Frames: 8-byte header `(key_id: uint32 big-endian, n_bytes: uint32 big-endian)` followed by payload.

- `key_id` 0–319: pixel timestamp data (`px_000.bin` … `px_319.bin`)
- `key_id` 320–325: sync signals — master/slave dwell, line, frame
- `0xFFFFFFFF` (KEY_SETUP): payload is UTF-8 output directory path — opens one acquisition session
- `0xFFFFFFFE` (KEY_END): empty payload — closes the session; `run_session_loop()` loops back for the next

Pixel mapping: `PIXMAP` in `sender_backend.py` maps lSPAD pixel indices to output keys. Slave pixels occupy indices 0–169, master pixels 170–319.

### Abnormal marker logging (sender)

A healthy timestream carries only photons (lSPAD ids `<150` master / `<170` slave), the coarse-counter reset `234`, and the dwell/line/frame markers `225`/`226`/`228` (`NORMAL_MARKER_IDS`). `sender_backend.py` reports every other id live over the control channel — FIFO overflow `247`, file-start `239`, and any id no pixel on that chip can emit (usually 7-byte record framing having slipped) — with the session record index and detector-relative timestamp, so a misplaced marker is distinguishable from an expected one.

Throttled deliberately: `log_fn` writes to the control socket from the parser thread, so a flood would stall the parser and cost real photons. First sighting of each `(chip, id)` logs at once, then one rollup line per id per `ANOM_LOG_S`; past `ANOM_MAX_FIRST` distinct ids it stops opening new lines. Per-id totals land in `stats['abnormal']` → `session_stats.json`.

### Live g² correlator

`correlate.py` integrates with `run_session_loop()` via `pixel_hooks: dict[key_id, list[queue.Queue]]`. Matching chunks are enqueued **in addition to** being written to disk — a read tap, not a diversion. `CorrelateWindow` accumulates int64 timestamps from two pixel queues and calls the Numba JIT `_multistart_multistop()` kernel in a background thread. The kernel is pre-warmed at startup to avoid the first-call JIT delay.

Each key fans out to **every** subscriber. `merge_hooks()` (`receiver.py`) composes the per-window `{key_id: Queue}` maps by appending rather than overwriting, so two windows watching one pixel both get every chunk, and a correlator watching key 320/323 is no longer clobbered by the dwell-calibration tap. The payload is an immutable `bytes`, so fan-out is zero-copy — subscribers must treat it as read-only. A bare `Queue` value is still accepted and normalized once per connection, so the correlator windows keep returning plain `{px: Queue}`. `ReceiverGUI._correlators` is the single list of windows: hook merging, the `is_enabled` calibration gate, and both `start_with_offset` paths all iterate it, so adding a window means editing one line.

Both correlator windows have a `Mark τ (ns)` field (default 14) that puts a marker on that bin and annotates its counts, excess over the mean, SNR and mean ± σ — the same numbers `tools/plot_g2_result.py` reports, but at a τ you name rather than at the argmax.

The receiver's **Write timestamps to disk** checkbox (on by default) sets `run_session_loop(write_hooked=...)`. Unchecked, hooked *pixel* keys go to the correlator queue only and their `px_*.bin` is never created — live correlation without keeping the timestamps. Keys 320–325 are never suppressed: they are hooked on every run for clock calibration, and the offline offset estimate needs them afterwards. The flag is read once per data connection, so toggling mid-run applies from the next START.

### SSH remote launch (`ssh_launcher.py`)

Automates sender node startup via paramiko password auth:
1. Find and start `lSPAD.exe` detached via WMI `Win32_Process.Create` (survives SSH disconnect)
2. Wait for lSPAD port 9999 to open
3. Apply pixel mask (`M,<path>`), run TDC calibration (`T,v,1` → `T,c,1`)
4. Git pull the repo, launch `sender.py` detached via venv `pythonw.exe`

### Data files

Stored under `spad_data/` (gitignored). Each acquisition session creates a subdirectory containing `px_000.bin` … `px_319.bin` (raw int64 timestamps per pixel) and `master_dwell.bin`, `slave_dwell.bin`, `master_line.bin`, `slave_line.bin`, `master_frame.bin`, `slave_frame.bin` (synchronization signals).
