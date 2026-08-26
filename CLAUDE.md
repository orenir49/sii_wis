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

# Raw lSPAD capture (Stage 2 Phase 0) -- set on the SENDER, off by default
$env:SII_WIS_RAW_DUMP = 'C:\spad\capture.raw'   # optional: SII_WIS_RAW_DUMP_MAX_MB (2048)
python tools\raw_dump.py --info spad_data\capture.raw

# Test suite (plain asserts; no pytest in requirements.txt)
.venv\Scripts\python.exe tests\test_epoch_fix.py
.venv\Scripts\python.exe run_log.py
.venv\Scripts\python.exe tests\test_hook_fanout.py
.venv\Scripts\python.exe tests\test_channel_graph.py
.venv\Scripts\python.exe tests\test_multi_window.py
.venv\Scripts\python.exe tests\test_write_lock.py
.venv\Scripts\python.exe tools\raw_dump.py --selftest
.venv\Scripts\python.exe correlate_kernel.py      # kernel equivalence
.venv\Scripts\python.exe synthetic_source.py      # generator + comb

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
| `run_log.py` | Per-run capture of the live log to `spad_data/log/<stamp>.log` — buffered during integration, flushed at the end; `--selftest` |
| `sender.py` | Sender GUI shell; starts command server thread |
| `sender_backend.py` | Command server + lSPAD TCP client; contains `PIXMAP` (320-pixel array mapping); logs abnormal marker ids live (see below) |
| `correlate.py` | `CorrelateWindow` (single-pair) live correlator, Numba JIT kernel, shared `_mark_peak_bin`/`pick_unit` helpers; "Compute R…" button opens `tools/sii_calculator.py`. `QuadCorrelateWindow` was deleted 2026-08-26 — the multi-pair window's `grid` mode subsumes its 2×2 workflow |
| `correlate_multi.py` | `MultiCorrelateWindow` — up to ~320 pairs, one plot + pair selector. Widgets only; the logic lives in the three modules below |
| `correlate_engine.py` | `ChannelGraph` — which events are safe to correlate and which must be kept. No Tk; 59 tests |
| `correlate_kernel.py` | `_pair_kernel` (`nogil`, bitwise identical to `_multistart_multistop`) + `PairPool`; `--selftest` |
| `synthetic_source.py` | Pulsed-laser / Poisson generator — drives the whole path with no detector attached |
| `tools/sii_calculator_backend.py` | Pure formulas: `<\|V\|^2>`, coherence time, bunching-excess `R`, required integration time |
| `tools/sii_calculator.py` | `SIICalculatorWindow` — interactive bunching-excess / integration-time calculator |
| `tools/plot_g2_result.py` | Peak-annotated g² histogram + count-distribution PNGs from a saved `{px1}_{px2}_{suffix}.txt` |
| `tools/analyze_g2_pairs_offline.py` | Offline g² for arbitrary pixel pairs with the robust slave-dwell clock offset (matches the live correlator) |
| `tools/raw_dump.py` | Reader + `--selftest` for the sender's env-gated raw lSPAD capture (`SII_WIS_RAW_DUMP`); length-prefixed chunks so a replay reproduces the original recv() boundaries |
| `tools/pair_map.py` | Pure (node-1, node-2) pair derivation — identity / grid (both mask-driven) and file for the GUI, plus affine for `align_arc --emit-pairs`; mask cross-check, `--selftest` |
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

Each key fans out to **every** subscriber. `merge_hooks()` (`receiver.py`) composes the per-window `{key_id: Queue}` maps by appending rather than overwriting, so two windows watching one pixel both get every chunk, and a correlator watching key 320/323 is no longer clobbered by the dwell-calibration tap. The payload is an immutable `bytes`, so fan-out is zero-copy — subscribers must treat it as read-only. A bare `Queue` value is still accepted and normalized once per connection, so the correlator windows keep returning plain `{px: Queue}`. `ReceiverGUI._correlators` is the single list of windows (now two: `CorrelateWindow` and `MultiCorrelateWindow`): hook merging, the `is_enabled` calibration gate, and both `start_with_offset` paths all iterate it, so adding a window means editing one line.

### Multi-pair correlator (`correlate_multi.py`)

`MultiCorrelateWindow` correlates many pairs at once — a diagonal (each node-1 pixel against its matched partner on node 2), or a full grid. The concerns are deliberately split so the hard parts are testable without Tk:

| module | owns | tested by |
|---|---|---|
| `tools/pair_map.py` | which pixels pair with which | `--selftest`, 44 checks |
| `correlate_engine.py` | which events are safe to correlate | `tests/test_channel_graph.py`, 59 checks |
| `correlate_kernel.py` | the histogram | `--selftest`, 25 checks |
| `correlate_multi.py` | widgets | `tests/test_multi_window.py`, 45 checks |

Pair modes — **three**, and each reads exactly one input:

| mode | input | pairs |
|---|---|---|
| **identity** | the two masks | `p2 = p1` over the pixels active on **both** nodes |
| **grid** | the two masks | outer product of the two active sets (`QuadCorrelateWindow`'s 2×2 workflow at any size) |
| **file** | a `pix1,pix2` CSV | whatever the CSV says |

Only the widgets the chosen mode actually reads are on screen (`_on_mode_change`), and changing mode invalidates the derived list. There is **no affine mode in the GUI**: an affine mapping is a *fit result*, not something to retype, so `align_arc.py --emit-pairs LO HI` writes the pair CSV the fit implies and the window loads it in file mode. `pair_map.derive('affine', …)` still exists because `align_arc.write_pair_list` calls it — one owner of the inversion, so the fit and the correlator cannot disagree about `a`, `b`, `FIT_CENTER` or the tie-rounding rule.

The mask fields are **read-only and pulled from the receiver's `NodePanel.mask_var`** (`get_masks_fn`), resolved to a local copy in `.claude/masks/`. The mask that matters is the one actually applied to the detector; a second editable copy here would only ever be a chance to disagree. A name with no local copy fails Derive loudly rather than deriving 320 pairs from an empty set.

A non-bijective mapping (affine with `a ≠ 1`) means a node-2 pixel can serve two pairs; channels are therefore keyed by **distinct pixel**, never by pair. **Enable stays disabled until Derive succeeds**, and Derive shows a preview table flagging shared channels, dropped out-of-range partners, pixels active on **one node only** (`one_sided` — no pair can form, so that part of the detector is not being correlated), and any pixel the mask has switched off (`masked_off`, now principally a file-mode concern: a CSV can name a pixel the mask is not passing, which is a guaranteed permanent stall).

Exclusion is a **relative** judgement — a channel is only costing coincidences while its partners are still delivering. When nothing at all has arrived for `idle_after_s` (3 s, deliberately far shorter than the 30 s `stall_grace_s`: "is anything arriving" is answered by the next poll, while "has this channel given up" must be slow enough not to condemn a bursty pixel) the graph sets `stream_idle`, clears every exclusion and reports `idle — no data arriving`: a normal end of acquisition must not read as `LOSING COINCIDENCES` forever. Genuine exclusions are kept in `exclusion_history` (cleared on `start()`) so going idle cannot erase the audit trail, and that history — not the live flags — is what the saved `.npz` records, since saving usually happens after the stream has stopped.

Retention (`ChannelGraph`) generalizes the two-pixel logic: a node-1 event is released only once **every** partner has been observed past `t1 + tmax`. The release point is a `last_ts` **watermark** — the newest timestamp ever seen — not `arr[-1]`, so a channel legitimately trimmed to size 0 no longer reads as silent. Genuinely silent partners are excluded only after `stall_grace_s` of wall clock or `stall_tolerance_ps` of detector-time lag, and every exclusion is reported: it means coincidences are being lost *now*, which the status line distinguishes from "waiting on node 2, N s behind (nothing lost)".

Scale figures ride along with each run: the status line reports kernel ms/batch (last and mean), peak buffer and peak process RSS, and the saved `.npz` meta carries `n_pairs`, `peak_buffer_bytes`, `peak_rss_bytes`, `kernel_s_total`, `kernel_batches` and `kernel_s_per_batch` — so "what did 40 pairs cost" is answerable from a saved file rather than a screenshot. The buffer high-water mark lives on `ChannelGraph.peak_nbytes` (updated in `drain_all`, cleared by `start()`), not in the window: sampling it from the UI poll would miss the gated polls, which are exactly the ones holding the most. Peak RSS reads Windows' own `PeakWorkingSetSize` via ctypes — no polling loop can miss a spike, and it avoids adding `psutil` to `requirements.txt`, which every sender node installs from.

Overload policy is **hold**: past the RAM cap the correlator stops draining, freezes, and reports in red. No subsampling, no silent skipping. What an overload *means* depends on the receiver's write-to-disk checkbox, which is pushed into every window via `set_write_to_disk()` — with writes on a backlog is a delay, with them off it is permanent photon loss.

Output is one batched `.npz` (`tau_ps`, `hist (N, nbins)`, `px1`, `px2`, counts, JSON `meta`), written `.tmp` then `os.replace()`. **Export pair → .txt** emits the legacy `{px1}_{px2}_{suffix}.txt` that `tools/plot_g2_result.py` reads.

**Synthetic source** (`synthetic_source.py`) drives the entire path with no detector: one pulse train shared by both nodes, so cross-node g² shows a comb at multiples of the repetition period. The comb *period* validates the clock scale on every pair simultaneously and the tooth at τ=0 validates the offset — but only **modulo the repetition period** (12.5 ns at 80 MHz), so it pins the fine offset, not the coarse one. Note the peak-marker SNR does not grow with integration time on a comb: it takes σ over the whole histogram and the teeth inflate σ, so both peak and σ scale with counts and the reading is a shape statistic. Its magnitude is set by bin width — `≈ sqrt(nbins / (n_teeth · tooth_width_bins))`, so ~15 at 20–50 ps bins but ~2 at 5 ns bins. Read the comb by tooth spacing and phase, not from that number.

Every correlator window marks the **tallest bin** automatically (`_mark_peak_bin` in `correlate.py`, shared by all of them) and annotates its τ, counts, excess over the mean, SNR and mean ± σ. Same bin and same numbers as `tools/plot_g2_result.py`, so a live window and a replotted saved file agree. There is no user-set τ: the old `Mark τ (ns)` entry and the multi-pair window's SNR-vs-pair sparkline were both removed — the multi-pair window is one plot, with the selected pair's peak SNR on its info line.

The receiver's **Write timestamps to disk** checkbox (on by default) sets `run_session_loop(write_hooked=...)`. Unchecked, **nothing is written at all** — no `px_*.bin`, no sync files, and the output directory is not even created. Hooked keys go to their correlator queues only; everything else is discarded. The suppression covers un-hooked pixels deliberately (sparing them would leave the ~1.28 GB/s write path in place whenever the correlator watches a subset, which is the normal case) and the sync keys too: they cost ~370 B/s against ~13 MB/s of timestamps, and with no photons kept there is nothing for an offline offset estimate to be applied to. Writing them was also actively harmful — fresh sync streams in a directory still holding an *earlier* run's `px_*.bin` read as one coherent dataset to any offline tool. Now a data directory either holds a whole run or was never touched, and a pre-existing directory is called out in the log. The flag is read once per data connection — and `run_session_loop` keeps that value for every back-to-back session on it — so the choice is **committed** from the accept onward. The checkbox is therefore **disabled** while either node has a data connection open or a session active (`NodePanel.write_flag_is_committed`, `_refresh_write_disk_lock`), with an on-screen reason and a one-line log entry on each transition. Locking on `_session_active` as well as the connection matters: START is sent synchronously, seconds before the sender connects back, so without it a toggle could land between the two nodes' accepts and write one node's pixels but not the other's — half a dataset. To change the flag, disconnect a node.

Every run's live log is also captured to **`spad_data/log/<stamp>.log`** (`run_log.py`, 16 checks). The stamped file is created empty at START, lines are **buffered in memory during integration** and flushed when it ends — the master is already writing up to ~1.3 GB/s and the sender's TCP window is what must not close. After the flush, lines append straight through, so the soft-stop drain messages and the session summary are kept. With write-to-disk off this file is the run's *only* record; an interrupted run leaves a stamped zero-length file, which is itself informative.

### SSH remote launch (`ssh_launcher.py`)

Automates sender node startup via paramiko password auth:
1. Find and start `lSPAD.exe` detached via WMI `Win32_Process.Create` (survives SSH disconnect)
2. Wait for lSPAD port 9999 to open
3. Apply pixel mask (`M,<path>`), run TDC calibration (`T,v,1` → `T,c,1`)
4. Git pull the repo, launch `sender.py` detached via venv `pythonw.exe`

### Data files

Stored under `spad_data/` (gitignored). Each acquisition session creates a subdirectory containing `px_000.bin` … `px_319.bin` (raw int64 timestamps per pixel) and `master_dwell.bin`, `slave_dwell.bin`, `master_line.bin`, `slave_line.bin`, `master_frame.bin`, `slave_frame.bin` (synchronization signals).
