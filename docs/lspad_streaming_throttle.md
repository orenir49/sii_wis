# lSPAD's own binary stream throttles under backlog -- independent of the consumer

## Context

The wire-encoding bake-off (`docs/raw_timestamp_wire_encoding_bakeoff.md`)
found `mask_sparse.txt` (81 active pixels/node, ~20 Mcps/node) fails almost
identically across baseline/raw/delta wire modes, even with Phase 1's
fused-slot bucketing active: parser lag grows at close to 1:1 with wall
clock in every mode. That left an open question -- is the ceiling in this
repo's own parsing pipeline (PIXMAP lookup, epoch correction, combining to
int64, per-pixel queueing, network forwarding to the master), or is it
inherent to how lSPAD.exe itself delivers its binary stream? This doc
answers that with a standalone script that bypasses `node_backend.py`
entirely, then narrows it further by comparing against lSPAD's own
native file-based acquisition mode (see "Comparison" below).

## Method

`python_tcp_stream_binary.py` (not part of the repo -- a throwaway test
script) connects directly to lSPAD's own TCP port (127.0.0.1:9999, the
`SB` binary-stream command from `LSPAD_CLI.md`) on the node PC itself. It
does the least possible work: split each fixed 7-byte record into one of
two output files by its first byte (master/slave chip flag) -- no PIXMAP,
no epoch/reset correction, no combining into a timestamp, no per-pixel
queueing, no network send. The split itself is one vectorized numpy pass
per received chunk, not a per-record Python loop, so it can't become an
artificial bottleneck of its own. Run with `mask_sparse.txt` already
applied and the TDC already calibrated (via master.py's Launch, node-only,
no acquisition started through master.py/node.py).

## Result: a clean 10s run keeps up completely

| | node1 | node2 |
|---|---|---|
| requested duration | 10.0 s | 10.0 s |
| actual wall-clock | 10.5 s | 11.1 s |
| total records | 175,085,374 | 199,746,799 |
| mean throughput | 16.60 M rec/s | 17.96 M rec/s |
| implied incident rate | ~17.5 Mcps | ~19.9 Mcps |

Both nodes drain at essentially line rate (~110-155 MB/s bursts, matching
the ~120-135 MB/s loopback figure from the 2026-08-16 no-parsing probe) with
negligible lag past the requested window. A script doing nothing but a
numpy chip-split comfortably handles the same mask/rate that the real
pipeline (with Phase 1 bucketing) could only sustain at ~4 Mcps before
falling badly behind. Short bursts are not where the real ceiling lives.

## Result: a 60s run reveals a U-shaped throughput curve on both nodes

| | node1 | node2 |
|---|---|---|
| requested duration | 60.0 s | 60.0 s |
| actual wall-clock | 548.9 s (9.15x) | 925.6 s (15.43x) |
| total records | 1,046,009,949 | 1,198,268,028 |
| mean throughput (whole run) | 1.91 M rec/s | 1.29 M rec/s |
| throughput floor | ~3.9-4.5 MB/s (t~200-260s) | ~1.6-2.5 MB/s (t~360-530s) |
| final-moment reacceleration | 4.0 -> 97.2 MB/s (t~260-548s) | 1.9 -> 98.6 MB/s (t~530-925s) |

Both nodes show the same shape: ~100-150 MB/s for the first ~20-26s
(matching the clean 10s run), then a sharp crash and a smooth decay down to
a low floor sustained for an extended stretch, followed by a dramatic
reacceleration back up to near line rate right before the DONE trailer.
Total records / the true ~60s acquisition window gives ~17.4 Mcps (node1)
and ~20.0 Mcps (node2) -- matching the clean 10s figures almost exactly.
So the *production* rate was constant the whole time; what varied wildly
was the *delivery* rate.

## Interpretation

A fixed-complexity consumer (same numpy split, same code path, every
chunk) cannot itself produce a backlog-size-dependent throughput curve --
it does identical work whether draining a small or a huge queue. The only
place a rate that depends on how much is queued can come from is
**lSPAD's own internal buffering/socket-send mechanism**: fast while the
backlog is small, throttling hard once it grows past some threshold, and
recovering as the backlog shrinks back toward zero once the acquisition
window closes and production stops. This reproduces independently on two
separate physical machines (different CPU tier, different USB-Ethernet
hardware, same lSPAD software/detector electronics), which points at the
shared lSPAD software/firmware rather than either node's own hardware.

This does not fully explain the real pipeline's ~4 Mcps ceiling on its own
terms -- the two measurements aren't directly comparable (the bake-off's
failing runs were always manually aborted, never let reach a natural
DONE, so there's no equivalent "final wall-clock time" figure to compare
against this script's numbers) -- but it does mean **the ceiling is not
exclusively, and maybe not even primarily, in this repo's own parsing
code**. Optimizing the bucketing loop or the wire encoding cannot fix a
throttle that happens below all of that, in lSPAD's own delivery of
already-produced data.

## Comparison: lSPAD's own GUI/file-based acquisition does not show the same collapse

To check whether this is inherent to lSPAD's data *generation* or specific
to the `SB` TCP-streaming *delivery* path, the same mask/rate was also run
directly through lSPAD's own GUI on node2, using its native `T,<ms>`
file-based timestamping mode (not `SB`) -- no TCP client involved at all,
node.py/master.py untouched. Requested duration: 60,000 ms, identical to
the dummy-script tests above.

lSPAD writes this mode as rotating ~52.4-52.5 MB files per chip under
`data\tdc\Run000\` (`data_master{NNN}.txt`, `data_slave{NNN}.txt`). File
creation timestamps give a clean, independent progress trace without
touching the running acquisition at all:

| | `SB` TCP streaming (this doc, above) | native GUI `T,`-mode file write |
|---|---|---|
| requested duration | 60.0 s | 60,000 ms (60.0 s) |
| actual wall-clock | 548.9 s (node1) / 925.6 s (node2) | ~106-108 s |
| overrun | 9.15x / 15.43x | ~1.8x |
| shape | fast start -> crash to a multi-minute floor -> reaccelerate at the very end | mild mid-run slowdown (~110-130 MB/s early, dropping to roughly half, ~55-65 MB/s, around the middle third), no comparable floor or collapse |

Both were run on node2 with the same `mask_sparse.txt` and the same
requested 60 s. File creation intervals do show *some* slowdown in the
middle third of the run (consistent with the same underlying pressure that
causes the `SB`-mode collapse), but nowhere close to the severity: no
multi-minute near-flat floor, and the whole acquisition still finishes in
roughly double the requested time rather than nine-to-fifteen times it.

**This reframes the finding above.** The backlog-dependent throughput
collapse is not simply "inherent to lSPAD" in a generic sense -- it is
specific to (or far worse in) the `SB` binary TCP-streaming interface that
this repo's entire pipeline is built on, compared to lSPAD's own native
file-writing acquisition path. Since `node_backend.py` has no choice but
to use `SB` (that is the interface this system's whole node/master
architecture is built around), this doesn't change what this repo's code
can do about the ceiling -- but it does tell us where to direct any
vendor conversation: the `SB` streaming implementation specifically, not
lSPAD's data acquisition or file-based storage path in general.

## A second TCP-streaming mode (`S,`, string/ASCII) shows the same collapse

lSPAD exposes a third acquisition path besides `SB` (binary streaming) and
`T,` (file-based): `S,<ms>`, string/ASCII streaming (one CSV-ish line per
event, per `LSPAD_CLI.md`). A vendor example script for this mode
(`python_tcp_stream.py`) accumulates the whole stream as one Python string
and parses it in a single slow post-hoc pass — too confounded by its own
consumption cost to isolate lSPAD's delivery rate, so a minimal analog
(`python_tcp_stream_string.py`: `bytes.split(b'\n')` per chunk, route each
line to a master/slave file by its first byte, no field parsing) was
written the same way the `SB`-mode dummy script was.

**10 s runs, node1, twice for reproducibility**: both already lagged badly
(7.79x and 7.28x the requested duration) at a flat, unchanging ~35-42 MB/s
— no burst-then-collapse shape at all, just a hard ceiling from the very
first second. That looked at first like a simple, constant bandwidth cap
(ASCII text is far more verbose per record than `SB`'s fixed 7 bytes), a
qualitatively different signature from `SB` mode's backlog-dependent
throttle.

**A 60 s run changed that reading.** Throughput peaked at ~54 MB/s around
t=20s, then decayed steadily: 44 -> 27 -> 20 -> 14 -> 12 MB/s through
t=20-48s, continuing down to a floor of ~6.5-7.2 MB/s that held from
roughly t=1300s onward. Terminated deliberately at **t=1535.7s (25.6x the
60s requested)** with 8530.4 MB / 500,882,084 combined records written —
this is a **lower bound**, not a completion, since the process was killed
rather than left to reach its own DONE trailer (unlike the `SB`-mode 60s
runs, which did reach DONE and could show a genuine final-moment
reacceleration; S-mode was never run long enough to see whether it has
one too). So the 10s tests' "flat ceiling" reading doesn't hold at scale —
`S,` mode shows the same qualitative backlog-dependent decay as `SB` mode,
just with a lower peak (~54 vs ~150 MB/s) and a somewhat higher floor
(~6.5-7 vs ~2-4 MB/s).

**Reframes the framing above once more.** Both of lSPAD's TCP-streaming
interfaces — binary and string — degrade under sustained backlog; only
the file-based `T,` mode (see the comparison section above) avoids it.
That is a stronger, cleaner statement than "the ceiling is inherent to
lSPAD's binary stream": it looks inherent to **streaming a live TCP
connection at all**, regardless of wire encoding, and specific to *not*
being the file-write path.

One more data point from the teardown: after killing the string-mode
script, lSPAD still had ~1.06 GB backlogged (it doesn't stop producing
just because a client disconnects, consistent with earlier findings).
Draining it with a plain socket-read-and-discard loop (zero parsing,
nothing but `recv()` in a loop) pulled it out at a steady **~7.9 MB/s** —
essentially the same as the script's own floor right before it was killed.
A consumer doing *nothing at all* hits the same ceiling as one doing line
splitting, which rules out the consumer side entirely for `S,` mode's
floor, the same way the numpy-vectorized split ruled it out for `SB`.

## Implications

- The Phase 1 bucketing fix and the wire-encoding bake-off (raw vs. delta)
  are both worth keeping regardless -- they reduce real work in this
  repo's own pipeline -- but neither should be expected to close a gap
  whose root cause sits inside lSPAD's own streaming implementation.
- A practical operating rule until this is understood further: keep
  acquisitions short enough, or the combined rate low enough, that the
  backlog never grows past whatever threshold triggers the throttle --
  the clean 10s runs above suggest that threshold sits somewhere between a
  10s and a 60s run at this mask/rate.
- This is a question for the vendor (SPADlambda/lSPAD) if it needs to be
  resolved rather than worked around: is this throttle a known behavior
  (e.g. an internal buffer tier that spills from RAM to something slower),
  a configurable parameter, or a bug.
- Worth re-running at a few intermediate durations (e.g. 20s, 30s, 40s) to
  locate the actual onset threshold more precisely, and confirming whether
  the floor value itself depends on active-pixel count or incident rate,
  before drawing more conclusions about where exactly the threshold sits.

## Files

The test script (`python_tcp_stream_binary.py`) is not part of this
repo -- a throwaway diagnostic kept on each node under `Downloads/`, not
tracked in git.
