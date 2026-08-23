# Scale-up to 80-pair diagonal live correlations

> Written 2026-08-20 against `ee4c18e`, re-anchored the same day against `8ec3c10`. **Line numbers
> in the body refer to the `8ec3c10` tree and are now stale wherever a stage has landed** — trust the
> per-stage status blocks and the code, not the line numbers.

---

## STATUS — 2026-08-23

**Branch `feat/multipair-correlation`, 5 commits ahead of `main`. `main` is untouched and remains
the stable 1v1 fallback.** Nothing here has been pushed.

| stage | state |
|---|---|
| **1a** tap fan-out | **DONE** — `d049f36` |
| **1b** write-to-disk checkbox | **PARTIAL** — shipped in `8ec3c10`; 3 deviations still open, one of which is a Stage 3 prerequisite |
| **2** sender throughput | **NOT STARTED** — deliberately deferred, see below |
| **3** multi-pair correlator | **DONE in software** — `ab9a8c2`, `2507d7e`, `6476c68`, `ed842df`. **Not yet validated on hardware**, and Quad not yet deleted |

```
ed842df  Stage 3: multi-pair live correlator with a synthetic pulsed-laser source
6476c68  Add the pair-parallel g2 kernel, proved equal to the reference
2507d7e  Extract the retention engine as a testable ChannelGraph, and fix it
ab9a8c2  Add tools/pair_map.py: pure pair derivation, shared with align_arc
d049f36  Stage 1a: fan pixel_hooks out to every subscriber
```

### Test suite — 149 checks, all passing as of `ed842df`

Plain asserts, no pytest (it is not in `requirements.txt`). **Run all of these before trusting any
change**; the whole suite takes ~2 minutes, most of it numba compiling.

```
.venv\Scripts\python.exe tests\test_epoch_fix.py         # 12  (on main)
.venv\Scripts\python.exe tests\test_hook_fanout.py       # 16  Stage 1a
.venv\Scripts\python.exe tests\test_channel_graph.py     # 41  retention
.venv\Scripts\python.exe tests\test_multi_window.py      # 30  end-to-end
.venv\Scripts\python.exe tools\pair_map.py --selftest    # 29  pair derivation
.venv\Scripts\python.exe correlate_kernel.py             # 25  kernel equivalence
.venv\Scripts\python.exe synthetic_source.py             #  8  generator + comb
```

### Architecture as built

Stage 3 split into four modules so the parts worth testing are testable without Tk. That split is
load-bearing, not cosmetic: the golden brute force caught a real bug during the port (the first
version trimmed node-2 arrays *before* correlating against them, dropping nearly every coincidence
while still drawing a plausible histogram). Keep it.

| module | owns | do not put in it |
|---|---|---|
| `tools/pair_map.py` | which pixels pair with which | anything stateful |
| `correlate_engine.py` | which events are safe to correlate | Tk, numba, matplotlib |
| `correlate_kernel.py` | the histogram | anything not bit-identical to `_multistart_multistop` |
| `correlate_multi.py` | widgets | logic worth a test |
| `synthetic_source.py` | photons, with no detector attached | — |

### Measured on this machine (16 cores)

- Pool vs serial, 80 pairs / 8.76M t1 events / `n_shift=5`: **0.257 s → 0.035 s, 7.35x**,
  bit-identical at 4, 8 and 16 workers.
- ≈ **5 core-seconds per second of data** at 80 pixels x 1 MHz — about a third of a 16-core master.
  This is the number that makes Stage 2 deferrable.
- `n_shift` default changed from 20 to **5**, per the coverage argument in "Performance" below.

---

## RESUME HERE — next session

In priority order. The first two are the only things standing between the current tree and a real
80-pixel run.

1. **Validate on hardware with the pulsed laser** (same train split onto both nodes — confirmed).
   This is the gating item: everything in Stage 3 is proven against a synthetic source only. Start at
   8–16 pairs, `identity` mode, and read the comb by tooth *spacing* and *position*. Two caveats
   discovered while building, both now unit tests and both worth re-reading before the bench session:
   a comb pins the clock offset only **modulo the repetition period** (12.5 ns at 80 MHz), and the
   `Mark τ` SNR box structurally caps near a few σ on a comb. See the Stage 3 status block.
2. **Stage 1b deviation 1 — widen `write_hooked` from hooked-only to all pixel keys.** One-line
   change to the `skipped_keys` comprehension plus a docstring. **This is a Stage 3 prerequisite**:
   as shipped, an active-but-unhooked pixel is still written, so the flag does not deliver the
   1.28 GB/s relief that motivated it. Without this, "disk flat at 80 pixels" only holds if every
   active pixel is in some window's pair list.
3. **Delete `QuadCorrelateWindow`** (`correlate.py`, the `QuadCorrelateWindow` class and `_Channel`)
   once (1) passes. It is kept *only* as the transitional cross-check. When it goes, drop the
   `_pick_unit` staticmethod alias, remove it from `ReceiverGUI._correlators`, and re-run the suite.
4. **Stage 2**, when pair count x rate actually demands it. Start with its Phase 0 scaffolding (the
   env-gated raw-stream dump), which needs detector time and therefore wants to be captured during a
   bench session you are already having.

**Smaller open items**, none blocking:

- The count-distribution radio (`correlate.py`'s `_draw_distribution`) was **not** absorbed into the
  multi-pair window. The marked-τ helpers, the Compute R button, the SNR sparkline and the hold-policy
  status line all were.
- Stage 1b deviations 2 and 3 (disable the checkbox while streaming; record it in
  `session_stats.json`) remain open.
- `ReceiverGUI._correlators` now has three windows. Adding a fourth means editing that one tuple —
  that was the point of the refactor — but re-check the four consumers if you add a window with a
  different interface.

### What Stage 2 is deferred *behind*, and why

Stage 2 is a pure sender-throughput optimization with **no correctness dependency** on the
correlator, and it only binds at ~80 pixels x 1 MHz. Validating the multi-pair engine against a
pulsed laser at 8–16 pairs does not need it, and the measured kernel cost above says the master is
not the bottleneck. Doing Stage 3 first put a testable window in front of the laser weeks earlier.
Nothing about Stage 2's design below has changed; it is written against `8ec3c10` line numbers and
will need re-anchoring when picked up.

---

> **What landed on `main` between the first draft and Stage 1a** (none of it from this plan except 1b):
>
> | commit | change | effect on this plan |
> |---|---|---|
> | `20a058a` | sender logs abnormal marker ids live | adds a per-chunk full-array pass Stage 2a must fold into its 512-bin histogram |
> | `3b6a53a` | `Mark τ (ns)` marker + SNR box in both correlator windows | absorbed by Stage 3's window |
> | `8ec3c10` | **write-to-disk checkbox** + `CLAUDE.md` correction | Stage 1b, but narrower than specified |
> | `759288c` | boundary-epoch correction decided per tick run | **fixes pre-existing bug 3** below |

## Context

The live correlator today handles at most 2 pixels per node (`QuadCorrelateWindow`, 4 pairwise
histograms). The goal is 80 active pixels per node correlating only the **diagonal** — each node-1
pixel against its matched partner on node 2 — so ~80 pairs, with one live plot and a selector to
toggle which pair is shown.

Three obstacles, addressed in that order:

1. **Disk writes are unsustainable.** `run_session_loop()` opens 326 handles and writes every
   payload (`receiver_backend.py:150-156, 186-195`). At 160 active pixels x 1 MHz that is
   ~1.28 GB/s, which no single disk sustains — so this is a *throughput* fix, not just a space fix.
   The comment at `receiver_backend.py:187-190` already names the failure mode: the write stalls,
   the sender's TCP window closes, and the loss resurfaces as detector FIFO overflow.
   **Partly addressed** by `8ec3c10`'s `write_hooked` flag, but only for *hooked* pixels — see 1b.
2. **Sender throughput.** Absolute-picosecond int64 forces 8 bytes/timestamp on the wire, and the
   per-pixel bucketing loop is O(chunk x N_pixels).
3. **The tap is single-consumer.** `pixel_hooks` is `dict[key_id, Queue]`; two consumers of one
   pixel silently clobber each other (admitted at `receiver.py:705-709`).

**Decisions taken:** Stage 1 (disk checkbox + tap fan-out) lands first, self-contained. `px_NNN.bin`
stays absolute int64 so offline tools are untouched. Diagonal supports two modes — identity
(`p2 = p1`) or affine fit. One live plot with a pair selector. **On overload, never degrade
silently — fail loudly.** `QuadCorrelateWindow` is **retired** — the new window subsumes it (its 2x2
grid becomes the full-grid pair mode), so Quad survives only as transitional test scaffolding and the
last Stage 3 commit deletes it.

### Three pre-existing bugs found along the way — ALL THREE NOW FIXED

**But two of them are only fixed in the *new* engine.** `QuadCorrelateWindow` still carries the
dim-channel bug, which is the one caveat on using it as a cross-check: where the two engines
disagree on a sparse or bursty channel, the **new** one is right. Quad results taken before
2026-08-23 with unequal pixel brightness are affected.

| bug | status |
|---|---|
| dim-channel coincidence loss | fixed in `correlate_engine.py` (`2507d7e`); **still present in Quad** |
| latent calibration clobber | fixed for real by `merge_hooks` (`d049f36`) |
| non-monotonic timestamps | fixed on `main` by `759288c`, with `tests/test_epoch_fix.py` |

- **Dim-channel coincidence loss** (`correlate.py:1052-1063`). `cut_for` drops any partner whose
  array is *currently empty* from the release-point `min`. `keep_for` legitimately empties a channel whose
  newest event is older than `next_t1 - tmax`. So a pixel sparse enough to deliver nothing during a
  poll interval is excluded, node-1 events are released without it, and its next chunk finds its
  partners already gone. Bright pixels are unaffected (the sender flushes every 0.2 s against a
  0.5 s poll, so fresh data is essentially always present), but sparse pixels lose real coincidences
  and the histogram still looks plausible. **This affects current 4-pair results with unequal pixel
  brightness.** Fix: gate release on a `last_ts` high-water mark — the newest timestamp ever
  observed — instead of `arr[-1]`. Bit-identical on busy channels, correct on sparse ones.
  **FIXED** in `correlate_engine.py`. Both failure modes are now demonstrated against a
  `LegacyGraph` that reproduces the old rule: it loses 35 of 103 coincidences on a 2-partner grid
  while the bright pair still looks perfect (which is how it survived), and returns `cut=0` where the
  fix returns 2 on a 1-partner diagonal. A test pins that both rules stay bit-identical on busy
  channels, so this cannot have changed existing 1v1 results.

- **Latent calibration clobber** (`receiver.py:441-442`). `hooks[320] = ...` overwrites any
  correlator that had asked for key 320. **FIXED** by `merge_hooks` in `d049f36`; covered by
  `tests/test_hook_fanout.py`.
- **Timestamps are already non-monotonic, occasionally.** The residual documented at
  `sender_backend.py:557-561` (the last record of each chip in a chunk has no successor, so a
  `coarse == 0xFFFF` record keeps an over-counted epoch) leaves a timestamp **+6.5536 ms in the
  future**; the next event on that pixel then lands *earlier*. At ~122 chunks/s x 2 chips x 1/65536
  this fires every few minutes. Both `spad_new.ipynb` and the correlator call `np.searchsorted` on
  these arrays, which assumes sorted input — so this is a latent correctness hazard in existing
  analysis, independent of any change here. Fixing it properly means carrying each chip's final
  record across chunks; that is separate work, but Stage 2's codec must **tolerate** it.

  **FIXED on `main`** by `759288c`, which decides the correction per 0xFFFF tick run rather than per
  adjacent pair — the pairwise version (`7eecfb5`) demoted a good record whenever two photons shared
  one tick, putting ~20.6k records 6.5536 ms in the *past* on the 2026-08-20 151x151 run. A
  documented end-of-chunk residual remains (~1/65536 per chip per chunk), so Stage 2's codec must
  still tolerate a negative delta, and `Channel.check_monotonic` counts violations rather than
  raising.

---

## Stage 1 — Optional disk writing + multi-subscriber tap

### 1a. `pixel_hooks` fan-out — LANDED

Implemented as specified below, on `feat/multipair-correlation`. `merge_hooks()` sits at
`receiver.py:44-64`; the normalization and fan-out are at `receiver_backend.py:134-139` and the
`for q in subs.get(key_id, ())` loop replacing the old single-`put`. The `self._correlators` tuple
landed with it, and all four per-window sites (both `get_hooks_fn` lambdas, the `is_enabled`
calibration gate, both `start_with_offset` paths) now iterate it instead of naming windows.

`tests/test_hook_fanout.py` covers it — 16 checks, all passing, driving a real
`socket.socketpair()` through `run_session_loop()` rather than mocking it. Verified: two windows on
one pixel both receive every chunk; the payload objects are identical (`a is b is c`), proving
zero-copy; legacy `{key: Queue}` input still works; a window on key 320 survives the calibration tap;
fan-out composes with `write_hooked=False` (no `px_*.bin`, both subscribers still fed, sync files
intact). Confirmed in the real GUI: with `CorrelateWindow` and `QuadCorrelateWindow` both on pixel
147, `get_hooks_fn()` returns 2 subscribers for key 147 where the old dict-merge returned 1.

`receiver_backend.py` — hooks become `dict[int, list[Queue]]`:

- Normalize **once per session**, just after the handles are opened (~`receiver_backend.py:157`), so
  the inner loop stays branch-light and legacy `{key: Queue}` callers keep working:
  ```python
  subs: dict[int, tuple] = {}
  if pixel_hooks:
      for kid, v in pixel_hooks.items():
          subs[kid] = tuple(v) if isinstance(v, (list, tuple)) else (v,)
  ```
- Replace `receiver_backend.py:200-201` with `for q in subs.get(key_id, ()): q.put(payload)`.
- Update the docstring at `receiver_backend.py:108-111`.

`payload` is an immutable `bytes` from `readall()` (`receiver_backend.py:57-67`), so fan-out at the
queue is genuinely zero-copy — but note the copy reappears downstream at `correlate.py:439` and
`correlate.py:725` (`np.frombuffer(raw).copy()`), so two windows watching one pixel each hold their own int64 array.
Cost of the loop itself is unmeasurable (a `Queue.put` is sub-microsecond, and only *hooked* keys
pay it); the real per-chunk ceiling is the pre-existing Python overhead in that loop.

`receiver.py` — replace the clobbering dict-merge with an append-merge helper (~`receiver.py:41`):

```python
def merge_hooks(*hook_maps) -> dict[int, list]:
    """Compose per-window {key_id: Queue} maps into {key_id: [Queue, ...]}.

    Append, never overwrite: two windows asking for the same (node, pixel) both
    get every chunk. The old dict-merge silently starved the loser.
    """
    merged: dict[int, list] = {}
    for m in hook_maps:
        for kid, v in (m or {}).items():
            qs = merged.setdefault(kid, [])
            for q in (v if isinstance(v, (list, tuple)) else (v,)):
                if all(q is not seen for seen in qs):   # identity dedupe
                    qs.append(q)
    return merged
```

- `receiver.py:737-738` / `749-750` → `merge_hooks(*(c.hooks_node1 for c in self._correlators))`
  (and `hooks_node2`), introducing a `self._correlators` tuple at `receiver.py:704-710` so adding a
  third window does not mean editing four call sites. Note `8ec3c10` added a third lambda
  (`get_write_hooked_fn`) to each of these two call sites, so there are now **six** places a new
  window touches — more reason to do the `self._correlators` refactor before Stage 3.
- `receiver.py:440-442` → `merge_hooks(self._get_hooks_fn() ..., {320: self._master_dwell_q, 323: self._dwell_q})`,
  which also fixes the latent clobber noted above.
- Each window keeps returning plain `{px: queue}` (`correlate.py:378-399`, `961-980`) — **no change
  to `CorrelateWindow` or `QuadCorrelateWindow`.** `merge_hooks` does the normalization.
- Delete the now-false comment at `receiver.py:705-709`. The `CLAUDE.md` "enqueued instead of
  written to disk" fix is **done** in `8ec3c10` — `CLAUDE.md:95` now reads "in addition to".
  `correlate.py:4-8` was already correct ("The tap is a copy, not a diversion") and needs no change.

### 1b. Write-to-disk checkbox — LANDED IN `8ec3c10`, NARROWER THAN SPECIFIED

Shipped as `write_hooked: bool = True` on `run_session_loop()` (`receiver_backend.py:99`), plus a
global **"Write timestamps to disk (uncheck: live correlation only)"** `Checkbutton` in the
`ReceiverGUI` `acq` frame, on by default.

**What matches this spec:**

- Only the 320-pixel loop is guarded (`receiver_backend.py:151-154`); the 6 sync files are always
  opened (`155-156`). Keys 320-325 can never be suppressed — enforced by the `k < 320` filter when
  `skipped_keys` is built (`receiver_backend.py:131-133`), which matters because `NodePanel` hooks
  320 and 323 on *every* run, so a naive "skip anything hooked" rule would have deleted the dwell
  files that `estimate_offset` needs.
- The `else: unknown += 1` branch no longer fires for deliberately-skipped keys — there is an
  `elif key_id in skipped_keys: skipped += n_bytes` arm at `receiver_backend.py:196-197`, so no
  spurious "unrecognised key_id" warning (`212-214`).
- The session summary reports the un-written megabytes (`receiver_backend.py:215-220`), and a
  once-per-session line up front names the suppressed pixels (`158-163`).
- Suppressed pixels get **no file at all** rather than an empty one, so an absent `px_147.bin` reads
  as "not recorded" instead of "this pixel saw nothing".
- `event_accum` (`receiver_backend.py:204-205`) is handle-independent, so the count-rate display
  keeps working — verified.
- `NodePanel.__init__` gained `get_write_hooked_fn`, passed at `receiver.py:739` / `751` alongside
  `get_hooks_fn`, read once per session in `_accept_data_thread` (`receiver.py:444-447`) and passed
  through at `463`.

**Three deviations, all still open:**

1. **Scope: hooked-only, not all pixels.** `skipped_keys = {k for k in pixel_hooks if k < 320}`
   (`receiver_backend.py:133`) suppresses only pixels a correlator is watching. An active but
   *un-hooked* pixel is still written. With today's 2-pixel mask hooked == active so the two are
   indistinguishable, but **at 80 pixels this does not deliver the 1.28 GB/s relief that motivated
   the stage.** Widening it to all pixel keys is a one-line change to that set comprehension plus a
   docstring edit; the log line and skipped-byte accounting already generalize. Decide whether the
   flag means "don't keep what I'm correlating" (as shipped) or "don't keep pixel data at all" (as
   specified) — Stage 3 needs the latter.
2. **Not locked during streaming.** The flag is read per node at accept time, seconds apart, so
   toggling in that window still desynchronizes the two nodes. Shipped mitigation is weaker: the
   toggle handler (`receiver.py:827-840`) logs which way it went and says it applies from the next
   START. Disabling the `Checkbutton` while any node is streaming is the actual fix and is
   unimplemented.
3. **Not recorded in `session_stats.json`.** `_record_session_stats` (`receiver.py:357-409`) does not
   carry the flag, so a directory with no `px_*.bin` is only explicable from the log. Note the stats
   dict itself originates on the *sender*, which has no idea about this receiver-side choice — so
   this has to be injected on the receiver side, not plumbed through the wire.

**Safety check (traced, still valid):** dwell calibration is 100% live-hook driven —
`receiver.py:441-442` → `_drain` (`477-488`) → `_poll_sparse_cal` (`1169-1203`) →
`_apply_sparse_dwell_offset` (`1205-1296`) never touches a file. Disabling pixel writes cannot break
it, and the sync files still allow offline re-derivation.

Note `root.resizable(False, False)` (`receiver.py:694`) — the `acq` frame absorbed the new checkbox
row without trouble, but a second added row is worth re-checking.

---

## Stage 2 — Sender throughput

### 2a. Kill the O(chunk x N_pixels) bucketing loop

`sender_backend.py:615-626` does a full boolean scan of the chunk *per active pixel* — 80 passes
over every chunk at 80 pixels.

**The sort key must be `uint16`, and this is the whole ballgame.** numpy's `kind='stable'` takes the
fast radix path only for narrow integer types; `pixel_nr` is `int32` (`sender_backend.py:512`), which
falls back to timsort. Measured on 8192 elements: `uint16` 0.021 ms vs `int32` **0.266 ms** — 12x
worse. So fuse chip and pixel id into one 16-bit slot key,
`slot = raw[:,1].astype(np.uint16) | (is_mast.astype(np.uint16) << 8)` (0-255 slave, 256-511 master),
and sort the whole chunk once — both chips, physical pixels and markers together.

Use **`np.bincount(slot, minlength=512)` + `cumsum`** for the group boundaries rather than
`searchsorted`: no gather needed, O(n + n_slots), and it hands you the present-slot set free via
`np.nonzero(counts)`, replacing `np.unique` (which itself sorts). The 512-bin histogram then
collapses several other per-chunk full-array passes into table lookups — the overflow count
(`:521`), the recognised-key / abnormal test (`:603-611`), the 6 marker mask scans (`:628-633`), and
the `events_since_flush` sum (`:626`).

**New since the first draft:** `20a058a` added the abnormal-id detection at `sender_backend.py:601-611`
— `phys_ok`, an `np.isin` against `NORMAL_MARKER_IDS`, and a second `np.isin` against
`KNOWN_MARKER_IDS` inside the `abnormal.any()` branch. That is 2-3 more full-array passes per chunk
than the draft accounted for, and it is *exactly* the shape the 512-bin histogram subsumes: every
one of those tests becomes a lookup over 512 slot counts instead of a scan over the chunk. Fold it
in rather than leaving it as a parallel code path. `report_abnormal` itself
(`sender_backend.py:284-321`) already works from `np.nonzero` indices and needs no change beyond
being handed slot-derived indices.

Measured end-to-end on the grouping step: 5.6x faster at 80 pixels, 6.2x at 170, and **flat in pixel
count** — which is the actual point. The comment at `sender_backend.py:478-481` justifying the recv
size ("one numpy call per active pixel regardless of array length") becomes false and must be
rewritten.

Correctness notes: **stable** is load-bearing (a stable sort by pid preserves original index order
within a group, making it provably identical to the old boolean gather — verified at 2/10/40/80/170
distinct pids). Switching to quicksort is 2x faster and silently corrupts every g2; say so in a
comment at the call site. Slices are *views* into the sorted array, so a single-event pixel pins its
whole 64 KB chunk until flush (~1.5 MB live at 1 MHz) — do not `.copy()`. Build the slot table from
`master_loc`/`slave_loc`/`SPECIAL_KEY` at import, never hardcoded, and assert the master 150-169
hole is preserved (slave 150-169 are valid pixels; master ids there are not).

### 2b. Delta-encode the wire

The arithmetic at `sender_backend.py:575` is already vectorized and is *not* the bottleneck; the cost
is that absolute ps forces int64 (8 bytes/event at `sender_backend.py:251-255`).

**Use explicit-length segments, not a sentinel.** A `0xFFFFFFFF` sentinel is actively unsafe:
`np.int64(-1).astype(np.uint32)` is `4294967295` with **no warning**, so a real delta of −1 ps is
bit-identical to the sentinel, and any encoder that casts before range-checking emits a payload that
decodes as structurally valid and numerically wrong. Negative deltas occur in practice (see the
epoch-residual bug above). Overhead is identical either way (4-byte length + 8-byte base = 4-byte
sentinel + 8-byte base = 12 B), so there is nothing to trade.

Payload for `key_id < 320`, little-endian, one or more concatenated segments:

```
segment := uint32 n_deltas | int64 base_ps | uint32 delta[n_deltas]
events  := base, base+d0, base+d0+d1, ...
```

A new segment starts exactly when the next delta would be `< 0` or `>= 2**32`. Segment count is
implicit — walk until the offset equals `n_bytes`, which makes the format **self-validating**: any
desync raises instead of producing plausible garbage. Read the `int64` base with
`struct.unpack_from('<Iq', ...)` (it sits at offset 4 mod 8); every numpy read is `<u4` at a
4-aligned offset. Base is **per frame**, which is the only option compatible with the existing
protocol and also means deltas never span a flush boundary.

Encoder: **don't insert escapes, split.** `np.nonzero((d < 0) | (d >= 1<<32))` finds every boundary
in one vectorized pass; `np.concatenate(([0], bad+1, [n]))` turns them into segment bounds; cast to
`uint32` only on slices already proven in range. The Python loop runs once per *segment*, which is
once per frame in the regime that matters. Measured 1253 MB/s encode, 250 M events/s decode — 0.6%
and 0.4% of one core respectively at 1 MHz, so encode stays on the parse thread inside `flush()`.

**Break-even is ~256 counts/s/pixel** (`exp(-λ·4.295 ms) = 1/3`). Below that the encoding is *worse*
than 8 bytes, asymptotically 1.5x. That is fine and needs no adaptive mode: at any rate that
stresses the link it is a clean 2.00x, and the pessimal regime (50 Hz/pixel across 80 pixels) totals
~45 kB/s. The encoding is worst exactly where volume is nil.

**Markers (320-325) stay absolute** — a few hundred events/s, nothing to save, and it keeps the
`estimate_offset` / sparse-cal path (`receiver.py:488`) entirely out of the blast radius. The rule is
one comparison, `key_id < 320`, derived from a single shared constant and commented at both sites.

Put the codec in a **new shared `wire_format.py`** imported by both backends, with an adversarial
round-trip self-test under `__main__` as the verification artifact. Duplicated constants with a "must
match" comment (`receiver_backend.py:26`) are tolerable for five integers and intolerable for a
codec — a one-sided edit is exactly how this corrupts data. `ssh_launcher.git_update()` pulls the
whole repo, so a new module reaches the senders automatically.

**Version negotiation: change the setup key, don't add a payload version byte.** A version byte
protects nothing — a stale receiver would happily write the delta payload into `px_NNN.bin`,
producing a file that is structurally valid int64 and numerically meaningless, and that survives all
the way to the notebook. The only frame a stale receiver *cannot* ignore is the setup frame, because
`receiver_backend.py:142-143` hard-raises on anything that is not `KEY_SETUP`. So add
`KEY_SETUP_V2 = 0xFFFFFFFD` carrying JSON `{"dir": ..., "fmt": ...}`; the receiver accepts plain
`KEY_SETUP` as `fmt='abs'` (a pre-pull sender keeps working correctly) and a stale receiver fails at
`:143` at session start, before a byte hits disk. This matters because skew is plausible in **both**
directions: `ssh_launcher.git_update()` (`ssh_launcher.py:241-256`) pulls the *sender* only, the
receiver is a manual checkout, and `ssh_launcher.py:445-447` already documents pre-pull-code skew.

Decode goes in `run_session_loop()`'s inner loop, after `readall` and after the skip decision.
Resolve "is this key wanted" **once per session** into a flat lookup, so a pixel that is neither
persisted nor hooked costs one socket read plus a segment-header walk — `skipped_keys`
(`receiver_backend.py:131-133`) is the natural place to hang that resolution, since it is already
computed once per connection. That walk (`scan_deltas`,
0.33 µs/payload) also gives the exact event count for `event_accum` — `n_bytes // 8`
(`receiver_backend.py:204-205`) is wrong by ~2x for delta payloads — and validates structure on every
pixel frame, including ones never decoded. With disk-writing on, decode-then-write keeps
`px_NNN.bin` absolute int64: confirmed necessary, since `spad_new.ipynb` memmaps it as
`dtype=np.int64` and calls `np.searchsorted`. `tools/plot_g2_result.py` never touches `.bin` at all.

Knock-on: hook queues now carry `np.ndarray` int64 rather than `bytes`, so the receiver decodes
uniformly and consumers never need to know which format a key uses. Three mechanical edits —
`correlate.py:439`, `correlate.py:725`, `receiver.py:488` — and a mutation audit confirms nothing
mutates dequeued arrays in place, so the existing `.copy()` calls can go.

Finally, `recv(57344)` (`sender_backend.py:470`) is `7 x 8192`; keep any change a multiple of 7 to
preserve the empty-`carry` property. Decide from the already-instrumented `stats['recv_mean_b']`
(`:682-683`) rather than up front: if it sits at ~57344 the socket is saturated and `7 x 65536` will
help. `FLUSH_EVERY` needs **no** change — it counts events across all pixels, so cadence is
unchanged as N grows; framing overhead at N=80/1 MHz is 0.8% of a flush. Also set `SO_RCVBUF`
(~4 MB) on `spad_sock` before `connect()` (`:359-361`), currently the OS default.

---

## Stage 3 — Multi-pair correlator (~80 diagonal pairs), replacing Quad

> **LANDED 2026-08-23** on `feat/multipair-correlation`, essentially as specified below, with
> `QuadCorrelateWindow` **not yet deleted** — it stays as the transitional cross-check until the new
> engine is validated on hardware, which is the last commit of the stage.
>
> | file | role | tests |
> |---|---|---|
> | `tools/pair_map.py` | pair derivation | 29 checks |
> | `correlate_engine.py` | retention (`ChannelGraph`) | 41 checks |
> | `correlate_kernel.py` | `_pair_kernel` + `PairPool` | 25 checks |
> | `correlate_multi.py` | the window | 30 checks |
> | `synthetic_source.py` | pulsed-laser / Poisson generator | 8 checks |
>
> **Measured:** the pool is 7.35x over serial on 16 cores at 80 pairs / 8.76M events / `n_shift=5`,
> bit-identical at 4, 8 and 16 workers — about 5 core-seconds per second of data at 80 pixels x
> 1 MHz, so ~32% of a 16-core master. `n_shift=5` is the new default, per the coverage argument below.
>
> **Two findings not anticipated by the plan**, both from the synthetic source:
>
> - **A pulsed comb pins the clock offset only modulo the repetition period** — 12.5 ns at 80 MHz. It
>   validates the *fine* offset and the clock *scale* (tooth spacing) on every pair at once, which is
>   exactly what a multi-pair sanity check needs, but it cannot catch a coarse offset error. A
>   test asserts both directions: a correct offset puts a tooth at tau = 0, and an offset wrong by
>   half a period moves the comb off it.
> - **The marked-tau SNR box reads only a few sigma on a comb, no matter how long you integrate.**
>   `_mark_tau_bin` takes mean and sigma over the whole histogram, which assumes a flat background
>   with one peak; a comb has ~9 equal teeth inside +-tmax and they inflate sigma, capping the SNR
>   near sqrt(n_teeth). Read the comb by tooth spacing and position, not from that box — it is
>   correct for the thermal bunching measurement it was built for.
>
> **Not yet done in this stage:** deleting Quad (`correlate.py:737-1214`) and the transitional
> `quad_compat` cross-check; the count-distribution view and the backlog note were absorbed only in
> part (the window has the marked-tau helpers, the Compute R button, a per-pair SNR sparkline reusing
> `_mark_tau_bin`'s statistic, and a hold-policy status line, but not the count-distribution radio).
> Hardware validation is outstanding by definition.

This window **replaces `QuadCorrelateWindow`**, which is retired once it lands. That has one
requirement consequence: Quad's workflow is a full **2x2 grid** (every node-1 pixel against every
node-2 pixel), not a diagonal, so the pair-list input needs a grid mode or that capability is lost.

### Pair list — four modes

| mode | pairs | covers |
|---|---|---|
| **identity diagonal** | `p2 = p1` over a range | the common matched-pixel case; bijective, 1 partner per channel |
| **affine diagonal** | `p2 = round(((p1-160) - b)/a + 160)`, inverting `align_arc.py:253-257` (unchanged) | matched *wavelength*; `a != 1` so **not** bijective — some node-2 pixels serve two pairs |
| **full grid** | outer product of two pixel lists | **the old Quad**, at any size |
| **file** | explicit `pix1,pix2` CSV | hand-tuned overrides |

The channel/adjacency model covers all four unchanged: channels are keyed by *distinct* pixel and
each min is taken over that channel's own partner list, so a diagonal channel has 1 partner and a
full-grid channel has N. Guard the derived pair count in the UI — full-grid mode is how someone
accidentally asks for 6400 pairs.

The new window must also absorb what `CorrelateWindow` has and Quad lacked, since it becomes the
primary tool: the count-distribution view (`correlate.py:636-678`), the **Compute R…** button into
`SIICalculatorWindow` (`correlate.py:245-247`, `319-321`), and the backlog note
(`correlate.py:465-499`). Keep `CorrelateWindow` itself — it is the simple single-pair path,
`set_correlate_pixel_fn` (`receiver.py:549-550`, `740`, `752`) targets it, and it stays useful as an
independent cross-check against the new engine.

**Also now required, from `3b6a53a`:** the `Mark τ (ns)` marker and its counts/excess/SNR/mean±σ box.
Both existing windows have it, and it is the single most useful live readout for a diagonal — at 80
pairs you are watching one bin, not a spectrum. The helpers are already module-level and window-
agnostic (`_parse_mark_tau_ps`, `_mark_tau_bin`, `MARK_TAU_NS_DEFAULT` at `correlate.py:62-127`), so
`correlate_multi.py` imports them rather than reimplementing. Two consequences: the per-pair SNR
sparkline proposed under "Display" below should reuse `_mark_tau_bin`'s statistic (same mean/σ over
the whole histogram) so the sparkline and the box can never disagree, and the `write=False`
display-refresh convention added alongside it (`correlate.py:633-634`, `1188-1189`) is the right
pattern for the new window's own re-render-on-parameter-change path.

Non-bijection quantified: `dp2/dp1 = 1/a`, so over an 80-pixel span `a = 1.01` gives ~1 collision and
`a = 1.05` gives ~4. A handful of node-2 channels serve two pairs — enough to require the shared
design, few enough that per-pair duplicate channels would only cost ~5% RAM as a fallback if the
shared retention proves troublesome.

Put the derivation in a new pure `tools/pair_map.py` (no Tk, no numba — `correlate.py:29` already
puts `tools/` on the path), and give `align_arc.py` an `--emit-pairs LO,HI` flag calling the same
helper, so the fit and the correlator can never disagree about `a`, `b`, `FIT_CENTER`, or the
tie-rounding rule. `correlate.py` must not own the alignment convention, and `align_arc.py` lives
under `.claude/skills/` — skill tooling, not on the app import path — so importing *from* it is the
wrong direction. Match `align_arc.py:189-190`'s `np.round` exactly: banker's rounding differs from
`floor(x+0.5)` on exact halves, and one flipped tie silently repoints a whole pair.

Input: a node-1 range plus `a`, `b`; the derived list shown in a preview table (p1, p2, shared-with,
in-mask?) with a summary, and **Enable stays disabled until Derive succeeds**. This preview is not
optional polish — 80 pairs derived from two floats is exactly where a sign error on `b` silently
correlates the wrong pixels all night, and with disk writes off the run is unrepeatable (and the
checkbox from 1b now makes that state one click away). Partners
outside 0-319 are **dropped, not clamped**, and listed. Cross-check against the node's mask file
(path already in the panel at `receiver.py:120-122`; per `gen_mask.py:40-43` — at the repo root, not
under `.claude/` — the file lists
*masked-off* locations, so active = `set(range(320)) - file`; `receiver.py:120-122`) and flag any derived pixel that is
masked off — that is a guaranteed permanent stall, and catching it at Derive time is far better than
discovering it an hour in.

### Channels and retention

Extract the retention engine out of Tk into a testable `_ChannelGraph` — this is where the real
correctness content lives, and it currently has no tests because it is welded to a `Toplevel`. One
`_Channel` (`correlate.py:701-734`) per **distinct** (node, pixel), never one per pair.

Generalize `cut_for`/`keep_for` (`correlate.py:1052-1087`) to `min` over each channel's actual partner
set. For the diagonal every channel has 1-2 partners, so this is O(N) per poll. **Invariant to
document:** every coincidence within `tmax` is counted exactly once — *disjointness* (each channel's
array is sliced into consecutive non-overlapping batches) plus *completeness* (a t1 event is
released only once every partner has been observed past `t1 + tmax`).

**The empty-partner exclusion has *opposite* failure modes depending on topology** — same code, and
neither matches its comment:

| topology | a partner looks empty | consequence |
|---|---|---|
| Quad, 2 partners per node-1 channel | excluded from the min; the other partner still sets a cut | t1 **is released** → coincidences **silently lost** |
| Diagonal, 1 partner per node-1 channel | `cuts == []` → `return 0` (`correlate.py:1063`) | nothing released → **stall**: correct, but RAM grows at r·8 B/s |

So under the diagonal the bug is not silent loss but an unbounded stall — safer, but it will look
like a hang. Both are fixed by the same two changes:

- **`last_ts` watermark** replacing `arr[-1]`. This removes the *spurious* empty case entirely: a
  channel trimmed to size 0 by `keep` keeps its watermark, so a low-rate partner no longer looks
  silent.
- **Bound genuine silence.** Declare a partner excluded only after it has delivered nothing for
  longer than a `stall_grace` (wall clock, default ~30 s) or its watermark lags the max across
  channels by more than a `stall_tolerance` (detector time, default ~5 s). A stalled channel accrues
  r·8 B/s, so 30 s at 1 Mcps caps exposure at ~240 MB and stops growing on exclusion. **Report it
  loudly** in the status line and mark the pair in the selector — a silent exclusion is exactly how
  the original bug survived.

**Whole-node lag needs its own diagnostic.** The release logic is correctly gated — the cut comes
from node 2's own newest timestamp, so if node 2 falls behind, t1 simply backlogs and the histogram
stalls rather than binning anything wrong (and the kernel's `0 <= b < nbins` test at
`correlate.py:47-48` is a second guard: an out-of-range tau can only be *missing*, never
mis-binned). But the current UI makes that state indistinguishable from "no photons": if no node-2
channel delivers, `_poll_data`'s `t2_has_data` gate (`correlate.py:1006-1009`) launches no correlation
at all and the display just freezes silently. And the asymmetric case is worse — when *some*
node-2 channels deliver and others don't, the delivering ones set the cut and the silent ones lose
those coincidences permanently, which is the same exclusion bug reached by a different route.

So the status line must distinguish, per node and per channel: *waiting on node 2* (gated, nothing
lost, backlog N seconds) from *node 2 channel X excluded* (coincidences being lost now). Report the
backlog in **detector time**, not wall clock, since late-but-correctly-timestamped data is only
delayed. This is the readout `CorrelateWindow` had (`correlate.py:465-499`) and Quad dropped.

Three more fixes fold in:
- **Move the offset subtraction to ingestion.** `correlate.py:1048-1050` does `ch.arr - offset` every
  poll — 2N full array copies on the Tk main thread. The offset is fixed for the session by
  `start_with_offset` (`correlate.py:985-994`), so subtract in `_Channel.drain` where a `.copy()`
  already happens. This is the likeliest GUI-freeze source at 160 channels.
- **Merge only when a release will actually happen.** `merge()` is unconditional
  (`correlate.py:1040-1041`) and concatenates the whole accumulation (`731-734`), so a stalled channel
  re-copies a growing array every poll — O(n²) memcpy precisely when you can least afford it. A 30 s
  stall at 8 MB/s copies ~7 GB. `CorrelateWindow`'s docstring (`correlate.py:142-147`) articulates this
  concern; Quad regressed it. Compute `last_ts` from the last *pending* chunk so a channel never has
  to merge just to report its watermark.
- **Guard monotonicity.** `merge`/`searchsorted` assume non-decreasing chunk order, unchecked today.
  At 160 channels a violation corrupts one pair and looks like physics — and per the epoch-residual
  bug above, violations are real. Add a cheap `chunk[0] >= arr[-1]` assert behind a debug flag.

### Performance: fix `n_shift` before adding parallelism

Kernel work = `n_pairs x 2*n_shift x len(t1)` (`correlate.py:37-49`, unchanged; each shift rescans all of `t1`).
At 80 pairs, `n_shift=20`, 1 MHz that is ~3.2e9 inner iterations per second of detector data —
roughly 1.2 s of wall-clock per second of data on 8 cores, i.e. permanently behind.

That is ~10 core-seconds per second of data, so it needs 10+ cores just to break even — feasible at
~60% load on a 16-core master, permanently behind on 8.

But `n_shift=20` is oversized here, and the units are worth being careful about: the default
`tmax_var` is `500000` **ps**, which is 500 **ns**, not 500 µs. At 1 MHz the mean spacing is 1 µs, so
only ~1 stop event lies within ±tmax while `n_shift=20` samples 40 neighbours — roughly 40x
over-coverage, with the outer bins structurally empty. `n_shift≈5` is still ample and cuts the work
4x, to ~2.5 core-seconds per data-second — comfortable even on 8 cores.

So: **derive a suggested `n_shift` from the measured rate and `tmax`, and show a read-only "τ
coverage ≈ ±n_shift/rate vs ±tmax" line** so the coupling is visible in both directions. The trap is
real in the other direction too: cost is linear in `n_shift` while full coverage of ±tmax costs
∝ `r²·tmax`, so a regime with larger `tmax` or higher rate can genuinely need a large `n_shift` and
become infeasible. Default the multi-window update interval to 1-2 s — interval affects display
latency only and drops nothing (`correlate.py:1016-1019`) — and surface the estimate in the UI.

Then parallelize with a **`ThreadPoolExecutor`, one task per pair** — plus a new single-pair
`@njit(nogil=True, cache=True)` kernel that finds its partner index by a **forward sweep** instead of
`np.searchsorted`.

Two prerequisites make this work, and both are easy to get wrong:
- `@njit(parallel=True)` at `correlate.py:37` has **no `nogil=True`** — numba only releases the GIL
  when asked. Without that flag a thread pool is a no-op.
- `np.searchsorted` at `correlate.py:1117` holds the GIL and costs ~`n1·log n2`; at 500k events x 80
  pairs that alone is over a second of GIL-bound work per cycle. Moving the index computation into
  the kernel is what leaves the pool with nothing but submit/future bookkeeping on the GIL.

The sweep is **bitwise identical** to the current kernel: after it, `j == np.searchsorted(t2, t1[i],
side='left')` — the default side used at `correlate.py:556` and `1117` — including with duplicate
timestamps on either side, since the sweep stops at the first element not `< ti` and never resets.
That exact equality is the lever for the whole regression suite, so **do not** hoist `1.0/bin_width`
into a multiply (a tempting ~1.5-2x win): a tau exactly on a bin edge can then land one bin over,
destroying the property.

**Why a pool beats a `prange` over pairs at this scale** (the reverse of the right answer for
thousands of tiny pairs): work per pair is proportional to that pixel's own count rate, and rates
across a spectrum vary by an order of magnitude between line and continuum. Numba's `prange` uses
*static* chunking, so 80 iterations over 16 threads is 5 contiguous each and one heavy chunk sets the
critical path — a 2-5x tail is realistic, and sorting doesn't help because contiguous chunking just
concentrates the heavy pairs. A pool dispatches dynamically and self-balances. There is also no cache
reuse to exploit (each pair reads its own two arrays), per-task overhead is ~50 µs against ~10⁵ µs of
work, and it avoids both the ragged `typed.List` plumbing and the nested-threading hazard (numba's
default `workqueue`/`omp` layers are unsafe under concurrent entry to a `parallel=True` function).

Leave `_multistart_multistop` otherwise byte-for-byte intact — it stays `CorrelateWindow`'s kernel
(with 1 pair there aren't enough pairs to fill the cores, so `n_shift` is the right parallel axis
there) and it is the reference the new kernel is proved equal to. Extend `_prewarm()` (`correlate.py:55-59`, unchanged) to
warm both kernels from **one** thread behind a module-level once-lock, gating the pool's first use on
it: 16 threads triggering the same compile serialize on numba's compile lock and, with `cache=True`,
race on the cache file. Both windows currently spawn their own prewarm thread
(`correlate.py:169`, `777`), and the new window would make three.

### Overload policy — fail loudly, per your choice

With the write-to-disk checkbox off the module docstring's promise (`correlate.py:10-11` and the
backlog note at `correlate.py:497-499`: "nothing is dropped… raw data is still complete on disk") is
**void** — falling behind becomes permanent
photon loss or an OOM that takes the GUI with it. So: a RAM cap with a **hold** policy — stop
draining, freeze the histogram, report in red with how far behind and how much was lost. No
subsampling, no silent skipping. Restore the backlog readout that `QuadCorrelateWindow` dropped
(`correlate.py:465-499`), and surface the write-to-disk state inside the correlator so the warning
can say the right thing. Correct the docstring.

**This is now live, not hypothetical:** `8ec3c10` shipped the checkbox, so both docstring claims can
already be false today, at 2 pixels, with no further work. The correlator has no idea the flag
exists — nothing is plumbed from `ReceiverGUI.write_disk_var` into either window. Correcting those
two docstrings and plumbing the flag is worth doing **now**, ahead of the rest of Stage 3, since the
promise is what someone reads before deciding an overload is harmless.

### Display

One matplotlib axes plus a pair selector (combobox / prev-next) choosing which pair is drawn. All
~80 histograms accumulate regardless of what is shown; only the selected one is redrawn. Memory is
trivial: nbins = 5001 x int64 x 80 pairs ≈ 3.2 MB. Crucially, restructure `_poll_results` so there is
**exactly one `draw_idle()` per batch** — today `_poll_results` calls `_update_plot` once per pair
(`correlate.py:1135-1141`) and each call does `tight_layout()` + `draw_idle()`
(`correlate.py:1186-1187`); call `tight_layout()` once at build time.

Optional cheap addition, since the pair set is a diagonal: a small second axes with an 80-point line
of peak SNR vs pair index, so you can see *which* pair to select without clicking through 80. That is
the natural at-a-glance view for a diagonal (a 2-D matrix is the wrong shape here) and costs one
80-point plot.

Put the new window in a new **`correlate_multi.py`**: `correlate.py` is already 1214 lines (up from
1109 — `3b6a53a` added the marked-τ helpers), and Quad
stays in place only as a transitional cross-check until the new engine is validated — the final
commit deletes `correlate.py:737-1214`. Promote `_pick_unit` from a `CorrelateWindow` staticmethod
(`correlate.py:593-603`) to module level (Quad reaches for it at `correlate.py:1171`, so keep the
staticmethod alias until Quad is gone); the marked-τ helpers at `correlate.py:62-127` are already
module-level and are the precedent for where shared plotting code belongs.
Refactor `receiver.py` to a `self._correlators` list — `is_enabled` is
checked at `receiver.py:893` and `start_with_offset` called at `1241-1242` and `1293-1294`, and
missing one of those sites is the classic "new window never receives its offset, histogram silently
stays empty" bug.

Output: **remove `_write_histogram` from the display path** (`correlate.py:1199-1214`) — a Python
f-string loop over ~5000 bins, full-overwrite, per pair per batch, on the Tk main thread, onto the
same disk as the acquisition. Replace with one batched `.npz` (`tau_ps`, `hist (N,nbins)`, `px1`,
`px2`, per-channel event counts, and a JSON `meta` with bin width/tmax/n_shift/offset/marked-τ/
write-to-disk state),
written to `.tmp` then `os.replace()` so an interrupted save cannot truncate a good archive.
Triggered by an explicit Save button plus an optional slow auto-save. Half of the motivation is
already partly addressed: `3b6a53a` added a `write=False` path so display-only refreshes no longer
rewrite the file (`correlate.py:1188-1189`), which removes the per-keystroke rewrites
but **not** the per-pair-per-batch ones this bullet is about — `_poll_results` still passes the
default `write=True` at `correlate.py:1141`.

Keep a **"Export selected pair → .txt"** button emitting the exact legacy
`{px1}_{px2}_{suffix}.txt` / `tau_ps\tcounts` format, because `tools/plot_g2_result.py` reads that
2-column file and parses the pixel pair from the *filename* (`plot_g2_result.py:30-41`). That keeps
the existing figure pipeline working with zero changes for the common single-pair case; optionally
teach `load_histogram`/`parse_label` an `.npz` + `--pair` branch (and an `--all` contact-sheet mode)
later.

---

## Verification

**Stage 1.**

> *1a is DONE* — see the Stage 1a status block above; `tests/test_hook_fanout.py` covers every item
> listed below, including the `a is b` zero-copy proof and the key-320 case.

*1a (as specified, now implemented).* Enable two correlator windows on the *same* pixel: both must receive data, and
the payload objects must be identical (`a is b`) proving zero-copy fan-out. A window hooking key 320
alongside calibration: both get every chunk (today one starves). Legacy `{key: Queue}` input still
works.

*1b (done for what shipped).* Verified headless against `socket.socketpair()` at `8ec3c10`: with the
flag off, hooked pixels get no `px_NNN.bin` (324 files rather than 326), un-hooked pixels are still
written, all 6 sync files are intact, every hook still receives its full payload, and no spurious
"unrecognised key_id" warning appears. `ReceiverGUI` was instantiated to confirm the checkbox reaches
both `NodePanel`s. **Not** yet verified: sync files byte-identical to a flag-on run, dwell
calibration end-to-end, `event_accum` totals across a real acquisition, and
`session_stats.json` carrying the flag (not implemented — see 1b deviation 3). Timing the same
synthetic stream both ways to quantify the write-path relief was not done and is the measurement
that would justify widening the flag to all pixels.

**Stage 2.** Raw detector bytes are not retained and two acquisitions are never the same photons, so
"run it twice and diff" is unavailable. Build two mechanisms instead, and build them *first*:

- **Phase 0 scaffolding, before touching anything.** Add an env-gated raw-stream dump right after
  `sender_backend.py:470-477` (~4 lines, off by default) and capture one real 30 s / 80-active-pixel
  session **with the current code**. Replaying that capture through the old and new parse paths
  offline must produce **byte-identical** `px_*.bin` and identical
  `stats['records'/'overflow'/'unknown'/'epoch_fixes']` — and now also `stats['abnormal']`, added by
  `20a058a`, which is a per-`chip:id` dict and therefore a strictly sharper invariant than the scalar
  counters: it pins *which* ids were seen, not just how many. This is what makes 2a provable rather
  than argued, and it runs on a laptop with no detector attached.
- **Shadow assert for the codec.** Env-gated inside `flush()`:
  `assert decode_deltas(payload).tobytes() == arr.tobytes()`. Run one full real acquisition with it
  on — that exercises the true distribution, including dim pixels, dwell boundaries, and (given
  enough runtime) the epoch residual. Keep the flag permanently as the regression harness.
- **Codec self-test** in `wire_format.py`'s `__main__`, over empty / single-event / duplicate
  timestamps / dense random walk / every-delta-oversized / negative absolute base / 1e15 ps span,
  plus randomized deliberately-unsorted int64 arrays, asserting `scan_deltas(p) == a.size` each
  time. **The three rows to guard forever:** delta `== 2**32 - 1` (must stay one segment), delta
  `== 2**32` (must split), and delta `== -1` (the case a sentinel scheme cannot distinguish).

Then measure before/after: `overflow` and `lag_max_s` (`sender_backend.py:583-592`) are the outcome;
`records` and total `px_*.bin` bytes are the invariants proving nothing was lost or double-counted.
Add a `raw_b`/`wire_b` ratio to `stats` and expect ~2.00x at the rates that matter. Also track
`queue_blocks`/`queue_max` (`:695-700`, expect blocks → 0) and the receiver's `write_s`
(`receiver_backend.py:215-220`). With the 1b checkbox off, `write_s` collapses for hooked pixels, so
**measure with writes on** or the comparison flatters the codec.

**Stage 3.**

> **Done / outstanding, as of `ed842df`.** Everything below is implemented and passing **except**
> where marked. The one category not covered is *anything requiring real detectors* — by
> construction, since the synthetic source exists precisely so the rest did not have to wait for
> bench time.
>
> | item | state |
> |---|---|
> | kernel equivalence | **done** — `correlate_kernel.py --selftest`, 25 checks |
> | golden brute force, diagonal + grid | **done** — 8,084 and 11,573 coincidences, exact |
> | sparse / stall matrix (i)(ii)(iii) | **done** — plus the pre-fix engine shown to fail (ii) |
> | whole-node lag, symmetric + asymmetric | **done** — catches up bit-identical |
> | synthetic source mode | **done** — `synthetic_source.py`, and it is what unlocked the rest |
> | display / save / legacy `.txt` export | **done** — `tests/test_multi_window.py` |
> | **transitional Quad cross-check (`quad_compat`)** | **NOT DONE** — judged redundant once the golden brute force existed, since it is a strictly better oracle and Quad carries the retention bug. Reinstate only if a hardware discrepancy needs bisecting |
> | **on hardware — the fan-out proof** | **NOT DONE** — needs detectors. Remember the `suffix` warning below |
> | **after Quad is deleted: re-run the suite** | **NOT DONE** — Quad still exists |
> | **kernel s/batch and peak RSS at 4 → 16 → 80 pairs** | **PARTIAL** — timing measured (7.35x, ~5 core-s per data-second at 80 pairs); peak RSS not recorded, and the sustainable `npairs x rate` for this machine is not yet in `CLAUDE.md` |

- *Kernel equivalence:* the new pair-parallel kernel must match `_multistart_multistop` **exactly**
  (`np.array_equal`, int64 — reordering integer accumulation is exact, so any difference is a bug).
  Sweep n1/n2 including 0 and `n_shift > n2`, and τ exactly on a bin edge.
- *Golden brute force — the primary oracle.* Small streams through the batched pipeline vs an
  O(n1·n2) double loop over the same neighbour window, with `tmax` chosen so coincidences straddle
  many batch boundaries. Exact equality proves no boundary loss and no double counting. **Since Quad
  is being retired, this is the real ground truth, and it is a better oracle than Quad ever was —
  Quad carries the retention bug.** Run it in both grid and diagonal topologies so the 1-partner and
  N-partner adjacency paths are both covered.
- *Transitional Quad cross-check, in **two** modes.* Worth doing while Quad still exists, purely to
  catch porting mistakes — but a naive "must match Quad exactly" assertion would force the retention
  bug to be preserved, so gate it: with a test-only `quad_compat=True` flag (use `arr[-1]`, exclude
  empty partners) assert **exact** equality on the 2x2 grid config, proving the port is faithful;
  with the fix on, assert new ≥ old element-wise, equal whenever no channel ever drained to empty.
  The fixed engine is strictly *more* complete. Note `_launch_correlation` spawns a thread
  (`correlate.py:1105-1109`), so the harness should call `_correlate_bg` inline. Both the flag and
  this test are scaffolding — they go away with Quad.
- *Sparse / stall matrix:* (i) a pixel masked off entirely → its pairs never accumulate, every other
  pair is bit-identical to a run without it, the stall is reported, RAM stops growing after the grace
  period; (ii) a pixel at 1/1000 the rate → its pair accumulates with **no** loss thanks to the
  `last_ts` fix, and the pre-fix code must be shown to fail this same case; (iii) a pixel that stops
  mid-run → grace trips, the message names it, the partner channel is released.
- *Whole-node lag:* delay **all** of node 2's chunks by several seconds, then resume. Assert the
  histogram stalls and then catches up to **bit-identical** results versus an undelayed run — proving
  the gating loses nothing — while the status line reports "waiting on node 2, N s behind" rather
  than freezing silently. Then the asymmetric version: delay only *half* of node 2's channels, and
  assert the undelayed pairs are unaffected while the delayed ones still end up complete (and would
  have lost counts pre-fix).
- *On hardware — the fan-out proof.* Run `CorrelateWindow` on a pixel pair that is also in the new
  window's list (or, while it still exists, Quad on two pixels in the list). Both subscribe to the
  same keys, so the shared histogram must be identical — validating fan-out, retention, and the new
  kernel at once on real data, and only *possible* after Stage 1. **Use different `suffix` values**
  (`correlate.py:221` vs `840`): both write `{px1}_{px2}_{suffix}.txt` (`correlate.py:687` and
  `1207`), so with matching suffixes they fight over one file and you would unknowingly diff it
  against itself. Then a 15-min run with writes off — now one checkbox click, per 1b: disk flat,
  sync files growing, backlog quiet, RAM plateauing at the predicted level. Note the shipped flag
  suppresses only *hooked* pixels, so "disk flat" holds only if every active pixel is in some
  window's pair list; otherwise expect the un-hooked ones to keep writing.
- *After Quad is deleted:* re-run the full suite with `receiver.py`'s `_correlators` list down to two
  windows, and confirm the prewarm once-lock, `start_with_offset` fan-out
  (`receiver.py:1241-1242`, `1293-1294`), and `is_enabled` check (`receiver.py:893`) all still cover
  every remaining window — a missed site there is the silent "histogram never fills" failure. Add
  `get_write_hooked_fn` (`receiver.py:739`, `751`) to that list of per-window wiring sites.
- *A synthetic source mode* (a debug button filling all channels from a Poisson generator with a
  planted correlated peak) makes the whole derive → accumulate → display → save → `plot_g2_result`
  path testable on a laptop without burning detector time. Cheap, and it unlocks every test above.
- Log kernel seconds/batch and peak RSS at 4 → 16 → 80 pairs and record the actual sustainable
  `npairs x rate` for this machine in `CLAUDE.md`.
