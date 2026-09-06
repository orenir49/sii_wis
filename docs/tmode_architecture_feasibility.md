# Could we build the acquisition pipeline around T-mode instead of SB?

## Motivation

`docs/lspad_streaming_throttle.md` found lSPAD's own native `T,<ms>`
file-based acquisition mode degrades far more gently under sustained load
(~1.8x overrun on a 60s request) than the `SB,<ms>` binary TCP-streaming
mode this repo's whole pipeline is built on (9.15x-15.43x overrun, same
mask/rate). That raises an obvious question: could `node_backend.py` be
rearchitected to drive lSPAD via `T,` and read its output files instead of
consuming `SB`'s TCP stream directly?

Switching acquisition modes doesn't remove the lag problem, it *relocates*
it: instead of "can we drain lSPAD's TCP socket fast enough," the question
becomes "can we notice, read, parse, and forward each rotating `.txt` file
fast enough." This doc is a first-pass measurement of that relocated cost,
using the real capture from the 2026-09-06 GUI test (`data\tdc\Run000\` on
node2, 340 files, ~17.8 GB, never deleted since it's lSPAD's own output).

## T-mode's file format

Rotating files per chip, `data_master{NNN}.txt` / `data_slave{NNN}.txt`,
~52.4-52.5 MB each. Two record shapes, distinguishable by field count:

- **File-start marker** (`239,<coarse>`, 2 fields): exactly one, the first
  line of `data_master000.txt`/`data_slave000.txt` only.
- **Everything else** (3 fields: `pixel,coarse,fine`): both real photon
  rows (`pixel` = a normal pixel number) and reset-marker rows (`pixel` =
  234, format `234,0,<seq>` where `<seq>` is a per-chip sequential count of
  resets seen so far).

**The reset marker is authoritative and exact.** On a real 52.4 MB capture:
54 rows with `pixel == 234`, and comparing against the raw `coarse` column
of pixel-only rows found exactly 54 decreases of magnitude ~-65535 (one
full epoch, 65536 counts) plus 7 unrelated decreases of magnitude -1 (plain
same-tick TDC ordering jitter, not a wrap). The 54 `234` rows line up
exactly with the 54 genuine wraps — verified row-by-row, e.g. `...,65535,
56100` -> `234,0,0` -> `35,0,88482`, the marker sitting exactly at the
epoch boundary. So epoch tracking from this text format should use
`cumsum(pixel == 234)`, not coarse-decrease detection — the latter has a
7/61 false-positive rate on this sample from ordinary jitter alone, a real
correctness risk this doc's own first draft benchmark got wrong before
being corrected.

Given that, reconstructing the same `time_ps` value node_backend.py builds
from the binary stream is straightforward: `epoch = cumsum(pixel==234)`
(inclusive), `time_ps = (epoch*65536 + coarse)*100_000 + fine`, then drop
the reset rows. Cross-file epoch continuity (each new file's first rows
continue the previous file's cumulative reset count) is an open detail not
measured here — a real implementation needs to carry that count across
files, mirroring what `correct_boundary_epochs()` already does across
chunks for the binary stream.

## Benchmark: single-threaded parse throughput

`bench_tmode_parse.py`, run on node2 against its own real files (not
downloaded elsewhere): peek the first line (the file-start marker), then
`pandas.read_csv` (C engine) the rest, compute `epoch`/`time_ps` as above.

| | first 4 files | next 6 files (steady state) |
|---|---|---|
| throughput | 66-78 MB/s | ~22 MB/s |
| records/s | ~5.2 M | ~1.5 M |

The fast first few files are almost certainly OS file-cache residue from
downloading those same files moments earlier for format inspection, not a
real effect — the steady-state ~22 MB/s across 6 back-to-back fresh files
is the number to trust. Read time dominates total time (16s of 17s for 10
files); the numpy transform itself is fast (524 MB/s, negligible).

**Disk I/O is not the bottleneck.** A raw `open().read()` of 10 different
files (no parsing) on the same node measured 1341-2480 MB/s — 60-100x
faster than the pandas parse rate. The ~22 MB/s ceiling is CSV-parsing
CPU cost specifically, not storage.

## Reading these numbers

Comparing against T-mode's own production rate from the GUI test
(~110-130 MB/s early, ~55-65 MB/s mid-run): a naive single-threaded,
one-file-at-a-time `pandas.read_csv` reader, at ~22 MB/s, would **not**
keep up on its own — it's slower than even T-mode's own worst observed
phase. Left as-is, this would still accumulate backlog over time, just far
more gently than `SB` mode's collapse to a ~2-4 MB/s floor.

But since disk I/O has ~60-100x headroom and the transform step has ~25x
headroom over the parse step, the parse step itself is the only thing that
needs to get faster, and there's an obvious, cheap lever: **files are
independent units and there are at least two independent chip streams
(master, slave) plus many sequential per-chip files** — parallelizing
across a handful of worker processes should scale close to linearly until
CPU cores run out, unlike `SB` mode's throttle which no amount of
consumer-side parallelism could fix (the constraint there is lSPAD's own
delivery, not consumption speed).

## Faster parser: polars closes the gap on its own

Two zero-effort options were tried before reaching for parallelism:

- **`np.fromstring(data, sep=',')`** (no new dependency): a dead end.
  `fromstring` with a text separator does not tolerate the newline between
  rows (fails even on a two-row toy example), so it needs a `bytes.replace
  (b'\n', b',')` pass first — and the underlying parser turned out to be
  scalar, not vectorized: **2.4 MB/s** on the real 52.4 MB file, *slower*
  than pandas. Confirmed empirically, not assumed — worth recording so it
  isn't retried later.
- **`polars.read_csv`** (added to node2's venv for this test, not yet in
  `requirements.txt`): **~378 MB/s combined** (read 1066 MB/s, transform
  587 MB/s) on 10 fresh files, each 52.4 MB parsed in ~0.12-0.16 s total.
  Roughly 13-17x pandas' ~22-30 MB/s, and comfortably above T-mode's own
  *peak* production rate (~130 MB/s), not just its floor.

**This changes the parallelization answer.** T-mode rotates a new file
every ~0.3-1 s (from the file-creation-timestamp trace in
`docs/lspad_streaming_throttle.md`); polars processes one file in
~0.12-0.16 s — faster than even the *fastest* file-arrival interval. A
single-threaded, sequential, one-file-at-a-time `polars` reader should
keep up with T-mode's production without needing multiprocessing across
files at all (polars already parallelizes internally within `read_csv`).
The two-phase parallel-epoch-offset design sketched earlier in this doc is
still a reasonable fallback if a slower environment or a busier mask ever
needs the extra headroom, but it is no longer the load-bearing plan.

(Record counts matched pandas' run on the same 10-file range to within 1
out of ~35M — negligible for a throughput comparison, not investigated
further.)

## Not yet measured / next steps

- Sustained multi-hundred-file polars throughput (this test covered 10
  files; worth confirming the ~378 MB/s figure holds, not just an early
  burst — the same caution that mattered for the SB-mode and file-creation
  measurements elsewhere in this investigation).
- Cross-file epoch continuity and file-arrival/completion detection
  (noticing a new file exists and is done being written) — not
  benchmarked here at all, and is real engineering regardless of parser
  choice.
- Whether T-mode's own production-rate floor (~55-65 MB/s) itself
  degrades further under a busier mask/rate than this test used.
- This is a feasibility probe, not a design: no decision to actually build
  a T-mode-based pipeline has been made, and `polars` is not yet in
  `requirements.txt` — it was added to node2's venv only for this
  benchmark.
