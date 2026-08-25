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

# SII bunching-excess / integration-time calculator
python tools\sii_calculator.py

# Peak-annotated g2 histogram + count-distribution figures from a saved correlate.py result
python tools\plot_g2_result.py figs\19-8-26\147_147_resolve_peak.txt --outdir figs\<DD-M-YY>

# Gaussian fits to the bunching peak of several runs at once, baseline-normalised overlay
# plus a peak-height / peak-width comparison table
python tools\fit_g2_gaussian.py figs\25-8-26\284_283_small_oap.txt ^
    figs\25-8-26\284_283_two_small_oap.txt --labels small_oap two_small_oap ^
    --outdir figs\<DD-M-YY> --prefix <tag>

# Offline g2 for one or more pixel pairs, robust offset (matches the live correlator) -- run
# interactively on real hardware, not in a sandboxed/CI shell (see the script's own docstring)
python tools\analyze_g2_pairs_offline.py --base spad_data\<dir> 147x147 147x168 168x147 168x168

# Same, but node-qualified pairs -- needed for same-node (intra-node) correlations.
# "1:241x1:242" is node1 px241 x node1 px242; a bare "147x168" still means node1 x node2.
python tools\analyze_g2_pairs_offline.py --base spad_data\<dir> --bin-width 100000 --tmax 5000000 ^
    --n-shift 50 --suffix <tag> --outdir figs\<DD-M-YY> 1:241x2:241 1:241x1:242
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
| `tools/fit_g2_gaussian.py` | Gaussian-on-baseline fits to several saved histograms; normalised overlay + peak height/width comparison. Averages the model over each bin, so the quoted FWHM is not bin-broadened |
| `tools/analyze_g2_pairs_offline.py` | Offline g² for arbitrary pixel pairs, cross-node *and* intra-node, with a four-clock dwell offset model (see below) |
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

One exception: lSPAD opens every stream with a file-start marker, once per chip, so a `239` in the first `FILE_START_HEAD_RECS` (16) records is expected. `drop_head_of_stream()` filters those out of both the live log and the session tally — a `239` past that point means the stream restarted mid-session and is still reported. It filters record indices rather than skipping the whole group, so a restart landing in the same chunk as the opening marker survives (`tests/test_file_start_filter.py`).

Throttled deliberately: `log_fn` writes to the control socket from the parser thread, so a flood would stall the parser and cost real photons. First sighting of each `(chip, id)` logs at once, then one rollup line per id per `ANOM_LOG_S`; past `ANOM_MAX_FIRST` distinct ids it stops opening new lines. Per-id totals land in `stats['abnormal']` → `session_stats.json`.

### Live g² correlator

`correlate.py` integrates with `run_session_loop()` via `pixel_hooks: dict[key_id, queue.Queue]`. Matching chunks are enqueued **in addition to** being written to disk — a read tap, not a diversion. `CorrelateWindow` accumulates int64 timestamps from two pixel queues and calls the Numba JIT `_multistart_multistop()` kernel in a background thread. The kernel is pre-warmed at startup to avoid the first-call JIT delay.

Both correlator windows mark the tallest bin as it stands at each redraw and label it in place with `τ · excess · SNR` — one line at the marker, no corner box. The bin is chosen per redraw rather than named in advance, so the label wanders while the histogram is still noise and settles once a peak grows. Mean and σ are taken over the whole histogram, peak bin included, so the numbers match `tools/plot_g2_result.py` exactly.

The receiver's **Write timestamps to disk** checkbox (on by default) sets `run_session_loop(write_hooked=...)`. Unchecked, hooked *pixel* keys go to the correlator queue only and their `px_*.bin` is never created — live correlation without keeping the timestamps. Keys 320–325 are never suppressed: they are hooked on every run for clock calibration, and the offline offset estimate needs them afterwards. The flag is read once per data connection, so toggling mid-run applies from the next START.

### SSH remote launch (`ssh_launcher.py`)

Automates sender node startup via paramiko password auth:
1. Find and start `lSPAD.exe` detached via WMI `Win32_Process.Create` (survives SSH disconnect)
2. Wait for lSPAD port 9999 to open
3. Apply pixel mask (`M,<path>`), run TDC calibration (`T,v,1` → `T,c,1`)
4. Git pull the repo, launch `sender.py` detached via venv `pythonw.exe`

### The four detector clocks (offline offsets)

There are **four** free-running clocks in a two-node run, not two: each node has a
master and a slave chip, and each records its own `master_dwell.bin` /
`slave_dwell.bin`. So an offline g² pair needs the *difference* of its two clocks'
offsets — only a pair on the same node **and** the same chip is genuinely
offset-free. `clock_shifts()` in `tools/analyze_g2_pairs_offline.py` measures all
four against a node1/slave reference via `offset_tools.estimate_offset` and prints
a closure check (the two routes to node2/master agreed to 588 ps on 23-8-26 data).

The trap: chip membership follows the PIXMAP **index** (`master_loc` = indices
170-319, `slave_loc` = 0-169, `sender_backend.py`), while `px_*.bin` is named by the
PIXMAP **value** (the loc). The two partitions differ, so adjacent locs can sit on
different chips — e.g. locs 241 and 243 are master but 242 is slave. Use
`chip_of_loc()`, never `loc < 170`. Measured intra-node master↔slave offsets are
small (~1-2 ns), so they are negligible at 100 ns bins but matter at 250 ps or 1 ns.

Note also that `create_multistart_multistop_chunked` labels each bin by its **left
edge**, not its centre: at 100 ns bins the bin labelled `tau=0` spans [0, +100 ns)
and the one labelled `-100000` spans [-100 ns, 0), so a true zero-delay peak is
split across those two bins.

### Data files

Stored under `spad_data/` (gitignored). Each acquisition session creates a subdirectory containing `px_000.bin` … `px_319.bin` (raw int64 timestamps per pixel) and `master_dwell.bin`, `slave_dwell.bin`, `master_line.bin`, `slave_line.bin`, `master_frame.bin`, `slave_frame.bin` (synchronization signals).

Saved g² histograms (`tau_ps`/`counts` text, from the live correlator or the offline
tool) live with the figures they produced, in `figs/<D-M-YY>/`, not in `spad_data/`;
`spad_data/` holds raw timestamps, intensity scans and dwell diagnostics only.
`logbook.md` indexes the figures by date.
