#!/usr/bin/env python
"""
Live SPAD acquisition module.

Importable by a GUI:
    from node_backend import connect_receiver, check_connection, run

Or run standalone:
    python node_backend.py --target-host <IP> --duration <s> [--test]
"""

import argparse
import json
import os
import select
import socket
import struct
import sys
import numpy as np
import threading
import queue
import time
import traceback

# ---------------------------------------------------------------------------
# Configuration (standalone defaults)
# ---------------------------------------------------------------------------
SPAD_HOST   = '127.0.0.1'
SPAD_PORT   = 9999
DURATION_S  = 1
TARGET_HOST = '10.7.136.94'
TARGET_PORT = 50007

# Pixel buffers flush on whichever bound arrives first: enough events to make a
# frame worth sending, or enough time that a slow source still reaches the live
# correlator. The old 1_000-event bound flushed on essentially every chunk.
FLUSH_EVERY       = 50_000
FLUSH_INTERVAL_S  = 0.2
QUEUE_MAXSIZE     = 200

# lSPAD command-protocol timings (seconds)
LSPAD_HANDSHAKE_S = 10.0    # banner / T,v,1 — never block forever on a wedged lSPAD

# Stage 2 Phase 0 scaffolding: env-gated verbatim capture of lSPAD's stream,
# OFF unless SII_WIS_RAW_DUMP names a file. It exists so a parser rewrite can be
# *proved* rather than argued: raw detector bytes are not otherwise retained and
# two acquisitions are never the same photons, so "run it twice and diff" is
# unavailable. Replaying one capture through the old and the new parse path must
# give byte-identical px_*.bin and identical stats counters.
#
# Chunks are length-prefixed ('<I' then the bytes) rather than concatenated: the
# parser carries a partial record across recv() boundaries, so preserving the
# original chunking is what lets a replay reproduce the original run exactly,
# not merely agree with another replay of itself.
RAW_DUMP_ENV     = 'SII_WIS_RAW_DUMP'
RAW_DUMP_MAX_ENV = 'SII_WIS_RAW_DUMP_MAX_MB'
RAW_DUMP_MAX_MB  = 2048.0   # bench PCs have finite disks; stop, log, keep parsing
STOP_CONFIRM_S    = 5.0     # hard-abort drain budget; a soft stop has none
DRAIN_REPORT_S    = 5.0     # progress cadence while draining after STOP
PRESTART_DRAIN_S  = 120.0   # budget for reading a stale backlog to silence;
                            # ~120 MB/s on loopback, so this covers ~14 GB
TDC_CALIB_S       = 180.0   # T,c,1 runs for minutes

# ---------------------------------------------------------------------------
# Physics
# ---------------------------------------------------------------------------
PS_PER_COUNT     = int((1 / 10e6) * 1e12)
COUNTS_PER_RESET = 2**16
TOP_COARSE       = COUNTS_PER_RESET - 1   # last tick of an epoch; see the reset fix in run()

# ---------------------------------------------------------------------------
# Pixel mapping
# ---------------------------------------------------------------------------
PIXMAP = np.array([
    190,230,138, 62,254,274,172, 96, 20,310,220,130, 54,182,264,164, 88, 12,302,208,
    122, 46,299,252,156, 80,  4,294,196,114, 38,262,242,148, 72,263,286,186,106, 30,174,
    232,140, 64,270,276,176, 98, 22,312,222,132, 56,210,266,166, 90, 14,304,212,124, 48,
    255,256,158, 82,  6,296,200,116, 40,278,244,150, 74,291,288,188,108, 32,218,236,142,
     66,279,280,178,100, 24,314,224,134, 58,226,268,168, 92, 16,306,214,126, 50,283,258,
    160, 84,  8,298,204,118, 42,271,248,152, 76,  0,290,192,110, 34,202,238,144, 68,235,282,
    180,102, 26,316,228,136, 60,234,272,170, 94, 18,308,216,128, 52,198,260,162, 86, 10,
    300,206,120, 44,247,250,154, 78,  2,292,194,112, 36,246,240,146, 70,307,284,184,104,
     28,318,267, 59,141,223,  7, 89,171,269, 37,119,201,315, 67,149,231, 15, 97,179,285, 45,
    127,209,295, 75,157,241, 23,105,187,301, 53,135,217,  1, 83,165,257, 31,113,195,243,
     61,143,225,  9, 91,173,273, 39,121,203,311, 69,151,233, 17, 99,181,289, 47,129,211,287,
     77,159,245, 25,107,189,305, 55,137,219,  3, 85,167,261, 33,115,197,303, 63,145,227, 11,
     93,175,277, 41,123,205,259, 71,153,237, 19,101,183,293, 49,131,213,319, 79,161,249,
     27,109,191,309, 57,139,221,  5, 87,169,265, 35,117,199,275, 65,147,229, 13, 95,177,281,
     43,125,207,251, 73,155,239, 21,103,185,297, 51,133,215,317, 81,163,253, 29,111,193,313,
])

SPECIAL = {225: 'dwell', 226: 'line', 228: 'frame'}
RESET_ID          = 234      # coarse-counter reset marker
OVERFLOW_ID       = 247      # detector FIFO overflow: photons already lost
FILE_START_ID     = 239      # lSPAD file/stream-start marker
KNOWN_MARKER_IDS  = np.array(sorted(SPECIAL) + [RESET_ID, OVERFLOW_ID])

# Traffic a healthy stream is *made of*: photons, the coarse-counter reset, and
# the dwell/line/frame sync markers. Every other id is abnormal and is reported
# live by report_abnormal() — including OVERFLOW_ID, which is "known" only in the
# sense that we know what it means.
NORMAL_MARKER_IDS = np.array(sorted(SPECIAL) + [RESET_ID])
MARKER_NAMES = {
    OVERFLOW_ID:   'FIFO overflow, photons already lost',
    FILE_START_ID: 'file-start marker',
}
ANOM_LOG_S     = 2.0   # min seconds between rollup lines for one (chip, id)
ANOM_MAX_FIRST = 40    # cap on distinct first-sighting lines per session

LAG_CHECK_S = 5.0      # how often to recompute parser lag
LAG_WARN_S  = 2.0      # lag above this means data is queueing up

master_loc = np.array([PIXMAP[170 + i] for i in range(150)])
slave_loc  = np.array([PIXMAP[i]       for i in range(170)])

# ---------------------------------------------------------------------------
# Wire protocol keys
# ---------------------------------------------------------------------------
SPECIAL_KEY = {
    ('master', 'dwell'): 320,
    ('master', 'line'):  321,
    ('master', 'frame'): 322,
    ('slave',  'dwell'): 323,
    ('slave',  'line'):  324,
    ('slave',  'frame'): 325,
}
KEY_SETUP     = 0xFFFFFFFF   # payload: utf-8 output directory
KEY_END       = 0xFFFFFFFE   # payload: empty — signals end of one session
KEY_INTENSITY = 326          # payload: utf-8 header + raw lSPAD `I` reply (px,count,px2,count2)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def connect_receiver(host: str, port: int) -> socket.socket:
    """Open a TCP connection to the receiver. Returns the connected socket."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def drain_lspad(sock: socket.socket, quiet_for: float = 0.5,
                cap: float = 5.0, keep: int = 8192) -> tuple[bytes, int]:
    """
    Read from lSPAD until it stays quiet for `quiet_for` s, or `cap` s elapse.
    Returns (head, total_bytes) — at most `keep` bytes are retained.

    lSPAD's command server fans a running acquisition out to *every* connected
    client, so command replies and stream data share one byte flow. Draining to
    silence is the only way to know an acquisition has actually stopped.

    Nothing accumulates the discarded bytes. Measured: no lSPAD command purges
    a buffered backlog — not STOP, a second STOP, N,0/N,1, a fresh SB, another
    acquisition mode, or even a POW,0/POW,1 power cycle, and it survives closing
    the socket. Reading is the only way to clear it, and reading is fast
    (~120 MB/s on loopback). Materialising it was not: holding 2.2 GB in a
    bytearray and then copying it with bytes() put ~3.5 GB live on a laptop and
    turned a 15 s drain into 270 s.
    """
    head     = b''
    total    = 0
    deadline = time.time() + cap
    while time.time() < deadline:
        r, _, _ = select.select([sock], [], [], quiet_for)
        if not r:
            break
        chunk = sock.recv(1 << 20)
        if not chunk:
            break
        if len(head) < keep:
            head += chunk[:keep - len(head)]
        total += len(chunk)
    return head, total


def correct_boundary_epochs(coarse, reset_arr, pixel_nr, is_mast) -> int:
    """Undo the over-counted epoch on top-of-range records, in place.

    lSPAD emits the reset marker (id 234) just *before* the final
    coarse=0xFFFF tick of the epoch it closes, so a photon in that tick gets
    the incremented epoch and lands one full reset period (6.5536 ms) in the
    future — which also breaks the sortedness np.searchsorted relies on
    downstream.

    A correctly assigned top-of-range record is the LAST record of its epoch,
    so it is stale iff the next record on the same chip still carries the same
    epoch. Two things must be skipped when looking for that successor:

      * the reset marker itself, which sits on the boundary carrying the
        pre-increment epoch, and
      * any same-tick partner — a second photon in the *same* 0xFFFF tick is
        by construction in the same epoch, so a pairwise test sees it as
        proof of staleness and demotes a perfectly good record by a full
        epoch. That is a real regression, not a hypothetical: on the
        2026-08-20 151x151 run it fired ~20.6k times across 1.1e10 records,
        and every one of those was an inversion rather than a repair. It
        scales as P(>=2 photons in a 100 ns tick), so it gets worse the
        brighter you run.

    So the unit of decision is a *run* of consecutive same-chip records that
    are all at 0xFFFF and share an epoch — one tick's worth of photons. The
    whole run is stale iff the first record after it carries that same epoch,
    and the verdict applies to every member.

    Residual: a run at the very end of a chip's records in this chunk has no
    successor here and is left alone. That matters only if it sits exactly at
    0xFFFF — 1/65536 per chip per chunk. Carrying records across chunks to
    close that costs more than the defect.

    Returns the number of records corrected.
    """
    n_fixed = 0
    not_reset = pixel_nr != RESET_ID
    for chip in (is_mast, ~is_mast):
        idx = np.nonzero(chip & not_reset)[0]
        m = idx.size
        if m < 2:
            continue
        r_chip = reset_arr[idx]
        is_top = coarse[idx] == TOP_COARSE
        if not is_top.any():
            continue

        # partner[k]: record k+1 continues k's tick, so k is not the run end.
        partner = np.zeros(m, dtype=bool)
        partner[:-1] = is_top[:-1] & is_top[1:] & (r_chip[1:] == r_chip[:-1])
        # run_end[k] = index of the last record in k's run (nearest
        # non-partner position at or after k), by reverse-accumulating a min.
        run_end = np.minimum.accumulate(
            np.where(~partner, np.arange(m), m)[::-1])[::-1]

        has_succ = run_end < m - 1
        succ     = np.where(has_succ, np.minimum(run_end + 1, m - 1), 0)
        stale    = is_top & has_succ & (r_chip[succ] == r_chip)

        n_stale = int(stale.sum())
        if n_stale:
            reset_arr[idx[stale]] -= 1
            n_fixed += n_stale
    return n_fixed


def is_text_reply(data: bytes) -> bool:
    """True if `data` looks like an lSPAD text reply rather than binary stream."""
    if not data:
        return False
    printable = sum(1 for c in data if 32 <= c < 127 or c in (9, 10, 13))
    return printable / len(data) > 0.9


def check_connection(sock: socket.socket) -> bool:
    """Return True if the socket appears to still be connected."""
    try:
        r, _, e = select.select([sock], [], [sock], 0)
        if e:
            return False
        if r:
            # Receiver never sends data; readable means the connection was closed.
            return len(sock.recv(1, socket.MSG_PEEK)) > 0
        return True
    except Exception:
        return False


def open_lspad_stream(duration: float, log_fn=print):
    """Connect to lSPAD, clear any leftover acquisition, check the TDC
    calibration, and start the stream (SB). Returns the streaming socket.

    Extracted so a replay harness can substitute a socket that re-serves a
    captured stream (tools/replay.py). Deliberately ONLY the handshake: the
    parse loop it feeds is the thing a parser rewrite has to be proved
    against, so that loop stays byte-for-byte where it was. Refactoring the
    code under test to make it testable would defeat the point.
    """
    spad_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    spad_sock.settimeout(LSPAD_HANDSHAKE_S)
    spad_sock.connect((SPAD_HOST, SPAD_PORT))
    # Clear any acquisition still running from a previous session before
    # touching the command protocol. lSPAD streams to every connected
    # client, so a leftover SB would be read as our command replies and
    # would desynchronise the 7-byte record framing for the whole run.
    # Sending STOP straight away lets one drain cover the banner, any
    # leftover stream and the STOP reply — three waits cost >1 s of the
    # sparse-cal window.
    spad_sock.sendall(b'STOP\n')
    t_pre = time.time()
    _, pre_n = drain_lspad(spad_sock, quiet_for=0.4, cap=PRESTART_DRAIN_S)
    if pre_n > 256:
        dt = time.time() - t_pre
        log_fn(f'pre-START STOP: discarded {pre_n / 1e6:,.0f} MB of '
               f'leftover stream in {dt:.1f} s before lSPAD went quiet\n')

    spad_sock.sendall(b'T,v,1\n')
    tdc_reply, tdc_n = drain_lspad(spad_sock, quiet_for=0.2,
                                   cap=LSPAD_HANDSHAKE_S)
    if not is_text_reply(tdc_reply):
        raise RuntimeError(
            f'lSPAD is still streaming: T,v,1 returned {tdc_n:,} bytes '
            'of binary data instead of a calibration state. A previous '
            'acquisition was not stopped — refusing to start, since the '
            'record framing would be desynchronised.')
    if tdc_reply.decode('utf8', errors='replace').strip() == 'TDC calibration is invalid':
        spad_sock.sendall(b'T,c,1\n')
        log_fn(drain_lspad(spad_sock, quiet_for=2.0, cap=TDC_CALIB_S)[0]
               .decode('utf8', errors='replace'))

    spad_sock.settimeout(None)   # stream loop drives its own select()
    spad_sock.sendall(f'SB,{int(duration * 1000)}\n'.encode('utf8'))
    return spad_sock


def run(sock: socket.socket,
        output_dir: str,
        duration: float,
        test_mode: bool,
        stop_event: threading.Event,
        log_fn=print,
        soft_event: threading.Event | None = None) -> dict:
    """
    Run one acquisition session over an already-connected socket.
    Sends KEY_SETUP, streams data chunks, then sends KEY_END.
    Does NOT close the socket — the caller owns it.

    Returns per-session counters: records parsed, FIFO-overflow markers (photons
    the detector dropped — unrecoverable, so they are totalled rather than just
    warned about), records with an unrecognised pixel number, parser lag (final
    and peak), and the send-queue high-water mark.

    stop_event ends the acquisition. If soft_event is also set, the stop is
    "soft": lSPAD is told to STOP so no new photons are acquired, but everything
    it has already buffered is parsed to completion and nothing is discarded —
    however long that takes. Clearing soft_event mid-drain escalates to a hard
    abort, which discards the remainder after STOP_CONFIRM_S.

    lag_max_s and queue_max exist to tell two very different causes of overflow
    apart. Overflow with both low means the detector's own readout is the
    ceiling. Lag climbing first, or queue_max approaching QUEUE_MAXSIZE, means
    the blocking sq.put() stalled the parser — so the ceiling is ours, and the
    photons were lost downstream of the detector rather than by it.
    """
    stats = {'records': 0, 'overflow': 0, 'unknown': 0, 'abnormal': {},
             'lag_s': 0.0, 'lag_max_s': 0.0,
             'queue_max': 0, 'queue_blocks': 0,
             'recv_calls': 0, 'recv_mean_b': 0, 'discarded_b': 0,
             'epoch_fixes': 0, 'stop_mode': 'duration',
             'first_ts': None, 'last_ts': None}

    def is_soft() -> bool:
        return soft_event is not None and soft_event.is_set()

    # --- session preamble -------------------------------------------------
    outdir_bytes = output_dir.encode('utf-8')
    sock.sendall(struct.pack('>II', KEY_SETUP, len(outdir_bytes)) + outdir_bytes)

    # --- per-run queue and buffers ----------------------------------------
    sq: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)

    bufs: dict = {loc: [] for loc in range(320)}
    for _chip in ('master', 'slave'):
        for _name in SPECIAL.values():
            bufs[(_chip, _name)] = []
    MARKER_BUF_KEYS = [k for k in bufs if not isinstance(k, int)]

    def flush(keys=None) -> None:
        """Coalesce the named buffers (all of them by default) into ONE blob.

        The wire format is unchanged — frames are simply concatenated before the
        write, which the receiver cannot tell apart from separate writes. One
        queue item and one sendall per flush instead of one per pixel: at 320
        active pixels that collapses 326 syscalls into 1. The old behaviour
        issued ~265k sendall/s at full rate and overfilled the 200-slot queue on
        a single flush, blocking the parser mid-flush — which stopped it reading
        the socket and pushed the loss into lSPAD's FIFO.
        """
        parts = []
        for key in (bufs if keys is None else keys):
            buf = bufs[key]
            if buf:
                arr     = np.concatenate(buf)
                key_id  = key if isinstance(key, int) else SPECIAL_KEY[key]
                payload = arr.tobytes()
                parts.append(struct.pack('>II', key_id, len(payload)))
                parts.append(payload)
                bufs[key] = []
        if parts:
            blob = b''.join(parts)
            # Depth *before* enqueuing, so a full queue is visible as such. The
            # put below blocks when the queue is full, and a blocked put is what
            # stops the parser reading the socket and pushes loss into lSPAD's
            # FIFO — so count the blocks directly rather than inferring them.
            depth = sq.qsize()
            if depth > stats['queue_max']:
                stats['queue_max'] = depth
            try:
                sq.put_nowait(blob)
            except queue.Full:
                stats['queue_blocks'] += 1
                sq.put(blob)

    # --- live abnormal-marker reporting -----------------------------------
    # Photons, the coarse-counter reset and the dwell/line/frame markers are the
    # normal traffic; anything else says something went wrong *now* — a FIFO
    # overflow, a file-start marker in mid-stream, an id no pixel on that chip
    # can emit (which usually means the 7-byte record framing has slipped).
    # Report it while the run is still going instead of only in the totals.
    #
    # Throttled on purpose: log_fn writes to the control socket from the parser
    # thread, so an unthrottled flood would stall the parser and cost real
    # photons — the very failure it would be reporting.
    anom: dict = {}          # 'chip:id' -> [total, pending, last_log_t]

    def report_abnormal(mask, pixel_nr, is_mast, time_ps, rec0) -> None:
        """Log abnormal ids in this chunk, at most one line per (chip, id) per
        ANOM_LOG_S. `rec0` is the session record index of the chunk's first
        record, so a marker's position in the stream is judgeable — a file-start
        at record 0 is expected, one at record 4,000,000 is not."""
        now  = time.time()
        t0   = stats['first_ts'] if stats['first_ts'] is not None else 0
        idx  = np.nonzero(mask)[0]
        code = pixel_nr[idx].astype(np.int64) * 2 + is_mast[idx]
        for c in np.unique(code):
            sel  = idx[code == c]
            pid  = int(c) >> 1
            chip = 'master' if int(c) & 1 else 'slave'
            key  = f'{chip}:{pid}'
            name = MARKER_NAMES.get(pid, 'unknown pixel/marker id')
            t_s  = (int(time_ps[sel[0]])  - t0) / 1e12
            t_e  = (int(time_ps[sel[-1]]) - t0) / 1e12
            ent  = anom.get(key)
            if ent is None:
                anom[key] = [int(sel.size), 0, now]
                if len(anom) <= ANOM_MAX_FIRST:
                    log_fn(f'ABNORMAL: {chip} id {pid} ({name}) x{sel.size:,} — '
                           f'first at record {rec0 + int(sel[0]):,}, '
                           f't=+{t_s:.6f} s\n')
                elif len(anom) == ANOM_MAX_FIRST + 1:
                    log_fn(f'ABNORMAL: over {ANOM_MAX_FIRST} distinct abnormal '
                           f'ids — the record framing is probably desynchronised. '
                           f'Further ids are counted in the session summary '
                           f'only.\n')
                continue
            ent[0] += int(sel.size)
            ent[1] += int(sel.size)
            if now - ent[2] >= ANOM_LOG_S:
                log_fn(f'ABNORMAL: {chip} id {pid} ({name}) x{ent[1]:,} more '
                       f'(total {ent[0]:,}), latest at record '
                       f'{rec0 + int(sel[-1]):,}, t=+{t_e:.6f} s\n')
                ent[1] = 0
                ent[2] = now

    def sender_fn() -> None:
        while True:
            blob = sq.get()
            try:
                if blob is None:
                    break
                sock.sendall(blob)
            except Exception as exc:
                log_fn(f'sender thread died on sendall: {exc!r}\n')
                return
            finally:
                # Always mark done, even on failure — an uncalled task_done()
                # made the sq.join() in teardown hang forever.
                sq.task_done()

    sender_thread = threading.Thread(target=sender_fn, daemon=True)
    sender_thread.start()

    events_since_flush = 0
    last_flush         = time.time()
    start = time.time()

    try:
        if test_mode:
            log_fn(f'[test] Streaming 1000 zero-timestamps/s for pixel 100 — {duration} s.')
            while not stop_event.is_set() and (time.time() - start) < duration:
                bufs[100].append(np.zeros(1000, dtype=np.int64))
                events_since_flush += 1000
                if events_since_flush >= FLUSH_EVERY:
                    flush()
                    events_since_flush = 0
                remaining = duration - (time.time() - start)
                stop_event.wait(timeout=min(1.0, max(0.0, remaining)))

        else:
            # lSPAD's own TCP command protocol — see LSPAD_CLI.md for the full command set.
            spad_sock = open_lspad_stream(duration, log_fn)

            reset_m     = 0
            reset_s     = 0
            carry       = b''
            first_chunk = True
            total_bytes = 0
            t_stream    = time.time()
            last_lag_check = t_stream

            raw_dump      = None      # env-gated verbatim capture, see RAW_DUMP_ENV
            raw_written   = 0
            raw_cap       = 0.0
            raw_capped    = False

            stopping      = False
            stop_deadline = None      # None while a soft stop is draining
            drain_start   = 0.0
            drain_at_stop = 0
            last_report   = 0.0

            try:
                while True:
                    # On abort, send STOP but keep parsing: whatever lSPAD has
                    # already buffered is real photon data, and discarding it
                    # (as a drain-to-silence does) throws away everything the
                    # parser had not yet caught up on. Exit when lSPAD goes
                    # quiet, which is also the proof that STOP took effect.
                    if stop_event.is_set() and not stopping:
                        soft = is_soft()
                        stats['stop_mode'] = 'soft' if soft else 'abort'
                        log_fn(('Soft stop — sending STOP to lSPAD, then draining '
                                'everything it has buffered. At a high count rate '
                                'this can take much longer than the acquisition; '
                                'press Abort to give up on the remainder.\n')
                               if soft else 'Aborted — sending STOP to lSPAD.\n')
                        try:
                            spad_sock.sendall(b'STOP\n')
                        except OSError as exc:
                            log_fn(f'STOP failed: {exc!r}\n')
                        stopping      = True
                        # None = drain to completion, discard nothing.
                        stop_deadline = None if soft else time.time() + STOP_CONFIRM_S
                        drain_start   = time.time()
                        drain_at_stop = total_bytes
                        last_report   = drain_start

                    # A soft stop is never a trap: a later Abort clears the soft
                    # flag, and the deadline applies from that moment.
                    if stopping and stop_deadline is None and not is_soft():
                        log_fn('Soft stop escalated to abort — giving up on the '
                               'remaining backlog.\n')
                        stats['stop_mode'] = 'soft_then_abort'   # ASCII: this lands in JSON
                        stop_deadline = time.time()

                    if stopping and time.time() - last_report >= DRAIN_REPORT_S:
                        last_report = time.time()
                        log_fn(f'Draining: {(total_bytes - drain_at_stop) / 1e6:,.0f} MB '
                               f'parsed since STOP, {last_report - drain_start:.0f} s '
                               f'elapsed, parser {stats["lag_s"]:.1f} s behind\n')

                    r, _, _ = select.select([spad_sock], [], [], 0.5)
                    if not r:
                        if stopping:
                            break        # quiet: acquisition has really ended
                        continue
                    if stopping and stop_deadline is not None and time.time() > stop_deadline:
                        # Drain the rest to silence rather than walking away.
                        # No lSPAD command purges its buffer and it survives a
                        # disconnect, so anything left here would be handed to
                        # the *next* START, which would then refuse to begin
                        # until it had cleared it. Discarding costs seconds;
                        # deferring it cost minutes and an aborted run.
                        t_lost = time.time()
                        _, lost_n = drain_lspad(spad_sock, quiet_for=0.5,
                                                cap=PRESTART_DRAIN_S)
                        stats['discarded_b'] += lost_n
                        log_fn(f'WARNING: lSPAD still streaming after STOP — '
                               f'{lost_n / 1e6:,.0f} MB discarded unparsed in '
                               f'{time.time() - t_lost:.1f} s. This is real photon '
                               f'loss: the parser was behind, so lSPAD had buffered '
                               f'more than we could parse. Use Soft stop to keep it.\n')
                        break
                    data = spad_sock.recv(57344)
                    if not data:
                        log_fn('lSPAD closed the stream socket (EOF)\n')
                        break
                    if first_chunk:
                        first_chunk = False
                        raw_path = os.environ.get(RAW_DUMP_ENV)
                        if raw_path:
                            try:
                                raw_dump = open(raw_path, 'wb')
                                raw_cap = float(os.environ.get(
                                    RAW_DUMP_MAX_ENV, RAW_DUMP_MAX_MB)) * 1e6
                                log_fn(f'RAW DUMP ON -> {raw_path} '
                                       f'(cap {raw_cap / 1e6:.0f} MB)\n')
                            except OSError as exc:
                                log_fn(f'raw dump could not be opened: {exc!r}\n')

                    if raw_dump is not None:
                        # Write before parsing, so the capture is what arrived
                        # rather than what we managed to interpret.
                        if raw_written + len(data) <= raw_cap:
                            raw_dump.write(struct.pack('<I', len(data)))
                            raw_dump.write(data)
                            raw_written += len(data)
                        elif not raw_capped:
                            raw_capped = True
                            log_fn(f'raw dump hit its {raw_cap / 1e6:.0f} MB cap '
                                   f'— capture truncated, parsing continues\n')

                    total_bytes += len(data)
                    # The parse loop has a large fixed cost per chunk (the
                    # grouping loop makes one numpy call per active pixel
                    # regardless of array length), so throughput depends
                    # strongly on how much recv() hands over at a time.
                    stats['recv_calls'] += 1

                    done  = data[-4:] == b'DONE'
                    error = data[-5:] == b'ERROR'
                    if error:
                        log_fn(f'lSPAD ERROR trailer after {total_bytes} B / '
                               f'{time.time() - t_stream:.1f} s\n')
                        log_fn(data[-160:].decode('utf8', errors='replace'))
                        break
                    if done:
                        # A genuine trailer leaves the preceding stream
                        # record-aligned; a chance 'DONE' inside binary counter
                        # data does not. aligned=False means false positive.
                        payload_len = len(carry) + len(data) - 4
                        log_fn(f'lSPAD DONE trailer after {total_bytes} B / '
                               f'{time.time() - t_stream:.1f} s, chunk={len(data)} B, '
                               f'carry={len(carry)} B, aligned='
                               f'{payload_len % 7 == 0}, tail={data[-24:]!r}\n')
                        data = data[:-4]

                    data       = carry + data
                    n_complete = (len(data) // 7) * 7
                    carry      = data[n_complete:]
                    if n_complete == 0:
                        if done:
                            break
                        continue

                    raw      = np.frombuffer(data[:n_complete], dtype=np.uint8).reshape(-1, 7)
                    is_mast  = raw[:, 0].astype(bool)
                    pixel_nr = raw[:, 1].astype(np.int32)
                    coarse   = (raw[:, 2].astype(np.int64) << 8)  | raw[:, 3].astype(np.int64)
                    fine     = ((raw[:, 4].astype(np.int64) << 16)
                              | (raw[:, 5].astype(np.int64) << 8)
                              |  raw[:, 6].astype(np.int64))

                    # FIFO overflow is the one loss that cannot be recovered
                    # afterwards, so total it for the session rather than
                    # letting per-chunk warnings scroll past.
                    n_overflow = int(np.sum(pixel_nr == 247))
                    if n_overflow:
                        stats['overflow'] += n_overflow
                    stats['records'] += len(raw)

                    cs_m = np.cumsum((is_mast  & (pixel_nr == 234)).astype(np.int64))
                    cs_s = np.cumsum((~is_mast & (pixel_nr == 234)).astype(np.int64))

                    cum_reset_m    = np.empty(len(raw), dtype=np.int64)
                    cum_reset_s    = np.empty(len(raw), dtype=np.int64)
                    cum_reset_m[0] = reset_m
                    cum_reset_s[0] = reset_s
                    cum_reset_m[1:] = reset_m + cs_m[:-1]
                    cum_reset_s[1:] = reset_s + cs_s[:-1]
                    reset_m += int(cs_m[-1])
                    reset_s += int(cs_s[-1])

                    reset_arr = np.where(is_mast, cum_reset_m, cum_reset_s)

                    # Undo the epoch over-count on top-of-range records; see
                    # correct_boundary_epochs() for why the decision is made per
                    # 0xFFFF tick rather than per adjacent pair.
                    stats['epoch_fixes'] += correct_boundary_epochs(
                        coarse, reset_arr, pixel_nr, is_mast)

                    time_ps   = (reset_arr * COUNTS_PER_RESET + coarse) * PS_PER_COUNT + fine

                    # Lag = wall-clock elapsed minus reconstructed detector time.
                    # It grows when the parser cannot keep up, which is what
                    # pushes loss into the detector's FIFO.
                    if stats['first_ts'] is None:
                        stats['first_ts'] = int(time_ps[0])
                    stats['last_ts'] = int(time_ps[-1])
                    if time.time() - last_lag_check >= LAG_CHECK_S:
                        last_lag_check = time.time()
                        lag = ((last_lag_check - t_stream)
                               - (stats['last_ts'] - stats['first_ts']) / 1e12)
                        stats['lag_s'] = round(lag, 2)
                        # lag_s is overwritten every LAG_CHECK_S, so a spike
                        # that recovers before the run ends leaves no trace in
                        # the final record. Keep the peak too.
                        if lag > stats['lag_max_s']:
                            stats['lag_max_s'] = round(lag, 2)
                        # Only meaningful while still acquiring. During a drain
                        # the lag is the backlog by definition, nothing is being
                        # lost, and the Draining line already reports it.
                        if lag > LAG_WARN_S and not stopping:
                            log_fn(f'WARNING: parser is {lag:.1f} s behind the '
                                   f'detector — data is queueing and photons will '
                                   f'be lost to FIFO overflow if this grows\n')

                    # Anything that is neither a physical pixel for this chip nor
                    # a normal marker is discarded by the loop below. Report it
                    # live; 'unknown' keeps its narrower meaning (not even a
                    # marker we have a name for) for the summary and JSON.
                    phys_ok  = ((is_mast & (pixel_nr < 150))
                                | (~is_mast & (pixel_nr < 170)))
                    abnormal = ~(phys_ok | np.isin(pixel_nr, NORMAL_MARKER_IDS))
                    if abnormal.any():
                        stats['unknown'] += int(
                            (abnormal & ~np.isin(pixel_nr, KNOWN_MARKER_IDS)).sum())
                        report_abnormal(abnormal, pixel_nr, is_mast, time_ps,
                                        stats['records'] - len(raw))

                    dwell_seen = False
                    for chip_flag, loc_map, n_phys, chip_name in (
                        (True,  master_loc, 150, 'master'),
                        (False, slave_loc,  170, 'slave'),
                    ):
                        chip_mask = is_mast if chip_flag else ~is_mast
                        phys_mask = chip_mask & (pixel_nr < n_phys)
                        if phys_mask.any():
                            phys_pid = pixel_nr[phys_mask]
                            phys_ts  = time_ps[phys_mask]
                            for uid in np.unique(phys_pid):
                                bufs[loc_map[uid]].append(phys_ts[phys_pid == uid])
                            events_since_flush += int(phys_mask.sum())

                        for sp_id, name in SPECIAL.items():
                            mask = chip_mask & (pixel_nr == sp_id)
                            if mask.any():
                                bufs[(chip_name, name)].append(time_ps[mask])
                                if name == 'dwell':
                                    dwell_seen = True

                    # Markers go out immediately — calibration needs them promptly
                    # and there are only a handful per second. Pixel buffers are
                    # flushed on a size OR time bound, so a high rate produces few
                    # large frames instead of hundreds of tiny ones, while a low
                    # rate still reaches the correlator within FLUSH_INTERVAL_S.
                    if dwell_seen:
                        flush(MARKER_BUF_KEYS)
                    now = time.time()
                    if (events_since_flush >= FLUSH_EVERY
                            or now - last_flush >= FLUSH_INTERVAL_S):
                        flush()
                        events_since_flush = 0
                        last_flush = now

                    if done:
                        break

                # Only surprising for an indefinite run: SB,0 should stream until
                # we abort. A fixed-duration run ending by itself is the normal
                # case, and warning about it every time trains people to ignore
                # the warnings that matter.
                if not stop_event.is_set() and duration <= 0:
                    log_fn(f'WARNING: stream ended without an abort after '
                           f'{total_bytes} B / {time.time() - t_stream:.1f} s '
                           f'— lSPAD ended an indefinite (SB,0) acquisition\n')
            finally:
                if raw_dump is not None:
                    raw_dump.close()
                    stats['raw_dump_b'] = int(raw_written)
                    log_fn(f'raw dump closed — {raw_written / 1e6:,.1f} MB captured'
                           + (' (TRUNCATED at the cap)' if raw_capped else '') + '\n')
                spad_sock.close()

    finally:
        flush()
        sq.join()
        sq.put(None)
        sender_thread.join(timeout=10)
        # Signal end of session; receiver loops back to await the next KEY_SETUP.
        try:
            sock.sendall(struct.pack('>II', KEY_END, 0))
        except OSError as exc:
            # Never let this mask an exception already propagating out of the
            # try block — that one is the real diagnosis.
            log_fn(f'KEY_END failed: {exc!r}\n')
            if sys.exc_info()[0] is None:
                raise

    elapsed = time.time() - start
    stats.pop('first_ts', None)
    stats.pop('last_ts', None)
    stats['elapsed_s'] = round(elapsed, 1)
    if stats['recv_calls']:
        stats['recv_mean_b'] = int(total_bytes / stats['recv_calls'])
    stats['abnormal'] = {k: v[0] for k, v in anom.items()}
    if anom:
        # Per-id totals, so a throttled live line is never the whole story.
        log_fn('Abnormal ids this session: '
               + ', '.join(f'{k} x{n:,}' for k, n in
                           sorted(stats['abnormal'].items(), key=lambda kv: -kv[1]))
               + '\n')
    if stats['overflow'] or stats['unknown']:
        log_fn(f'WARNING: {stats["overflow"]:,} FIFO overflow event(s) — those '
               f'photons were dropped by the detector and cannot be recovered; '
               f'{stats["unknown"]:,} record(s) had an unrecognised pixel number\n')
    if stats['queue_blocks']:
        # Overflow with blocks is our fault, not the detector's — say which.
        log_fn(f'WARNING: the send queue was full {stats["queue_blocks"]:,} time(s) '
               f'(peak depth {stats["queue_max"]}/{QUEUE_MAXSIZE}) — the parser was '
               f'stalled waiting on the receiver, so any FIFO overflow above was '
               f'caused downstream of the detector, not by it\n')
    log_fn(f'Done. Elapsed: {elapsed:.1f} s — {stats["records"]:,} records, '
           f'{stats["overflow"]:,} overflow, lag {stats["lag_s"]:.1f} s '
           f'(peak {stats["lag_max_s"]:.1f} s), queue peak '
           f'{stats["queue_max"]}/{QUEUE_MAXSIZE}, '
           f'{stats["recv_calls"]:,} recv of {stats["recv_mean_b"]:,} B mean')
    return stats


def run_intensity(sock: socket.socket, output_dir: str, duration: float,
                   log_fn=print) -> int:
    """
    Run one classical intensity measurement (lSPAD's `I` command) and relay
    the raw reply to the receiver as a single KEY_INTENSITY chunk.

    lSPAD's reply (160 lines of `px,count,px2,count2`) is passed through
    unmodified — this is the same layout the spectral-align skill's
    align_arc.py expects (comma-separated, HEADER_ROWS header lines then
    4-column rows), so a 3-line header is prepended rather than reformatting
    the data itself.

    Sends KEY_SETUP, one chunk (header + raw reply), then KEY_END — the
    receiver's run_intensity_session() expects exactly this framing and
    writes the chunk to a single file, with none of run_session_loop()'s
    per-pixel bookkeeping (an intensity measurement carries no pixel stream).

    Returns the number of data lines written.
    """
    outdir_bytes = output_dir.encode('utf-8')
    sock.sendall(struct.pack('>II', KEY_SETUP, len(outdir_bytes)) + outdir_bytes)

    try:
        spad_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        spad_sock.settimeout(LSPAD_HANDSHAKE_S)
        spad_sock.connect((SPAD_HOST, SPAD_PORT))
        try:
            # Clear any leftover acquisition before issuing I — same reasoning
            # as run()'s pre-START STOP: lSPAD streams to every connected
            # client, so a stale backlog would otherwise be read as part of
            # the I reply.
            spad_sock.sendall(b'STOP\n')
            drain_lspad(spad_sock, quiet_for=0.4, cap=PRESTART_DRAIN_S)

            ms = int(duration * 1000)
            spad_sock.sendall(f'I,{ms}\n'.encode('utf8'))

            # lSPAD blocks silently for the whole measurement before replying
            # at all, so drain_lspad's "read until quiet" can't wait for the
            # first byte — it would see silence immediately (nothing has been
            # sent yet) and return empty well before the measurement is done.
            # Block for the reply explicitly, then mop up any trailing bytes.
            wait_s = duration + LSPAD_HANDSHAKE_S
            spad_sock.settimeout(wait_s)
            try:
                first = spad_sock.recv(1 << 16)
            except socket.timeout:
                raise RuntimeError(
                    f'lSPAD did not reply to I,{ms} within {wait_s:.0f} s '
                    '(measurement time + handshake margin) — check lSPAD is running.')
            if not first:
                raise RuntimeError(f'lSPAD closed the connection with no reply to I,{ms}')
            more, more_n = drain_lspad(spad_sock, quiet_for=0.3, cap=2.0, keep=1 << 16)
            reply   = first + more
            n_bytes = len(first) + more_n
            if not is_text_reply(reply):
                raise RuntimeError(
                    f'lSPAD is still streaming: I,{ms} returned {n_bytes:,} '
                    'bytes of binary data instead of an intensity reply. A '
                    'previous acquisition was not stopped.')

            n_lines = reply.count(b'\n')
            header = (f'# Classical intensity measurement (lSPAD `I` command)\n'
                      f'# duration_ms={ms}\n'
                      f'# pixel,counts,pixel,counts\n').encode('utf8')
            payload = header + reply
            sock.sendall(struct.pack('>II', KEY_INTENSITY, len(payload)) + payload)
            log_fn(f'Intensity measurement done — {n_lines} line(s).\n')
            return n_lines
        finally:
            spad_sock.close()
    finally:
        try:
            sock.sendall(struct.pack('>II', KEY_END, 0))
        except OSError as exc:
            log_fn(f'KEY_END failed: {exc!r}\n')
            if sys.exc_info()[0] is None:
                raise


# ---------------------------------------------------------------------------
# Command server  (receiver GUI drives acquisitions remotely)
# ---------------------------------------------------------------------------
DEFAULT_CMD_PORT = 50010


def run_command_server(cmd_port: int = DEFAULT_CMD_PORT,
                       status_fn=print) -> None:
    """
    Bind cmd_port and accept controller connections indefinitely.
    status_fn receives dict events: {'event': ..., ...}
    Call in a daemon thread.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('', cmd_port))
    server.listen(1)
    status_fn({'event': 'listening', 'port': cmd_port})

    while True:
        try:
            conn, addr = server.accept()
        except OSError:
            break
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        conn.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30_000, 5_000))  # 30 s idle, probe every 5 s
        status_fn({'event': 'ctrl_connected', 'addr': addr[0]})
        _send_ctrl_msg(conn, threading.Lock(),
                       {'status': 'log', 'msg': f'sender build {_build_id()}\n'})
        _handle_controller(conn, status_fn)
        status_fn({'event': 'ctrl_disconnected'})


def _build_id() -> str:
    """Identify the running code: git HEAD + pid.

    A stale node.py can keep the command port after a git pull, so the
    receiver needs to see which build is actually answering.
    """
    sha = '?'
    try:
        import subprocess
        sha = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        ).stdout.strip() or '?'
    except Exception:
        pass
    return f'{sha} pid {os.getpid()}'


def _send_ctrl_msg(conn: socket.socket, lock: threading.Lock,
                   msg: dict) -> None:
    data = (json.dumps(msg) + '\n').encode()
    with lock:
        try:
            conn.sendall(data)
        except OSError:
            pass


def _handle_controller(conn: socket.socket, status_fn) -> None:
    lock        = threading.Lock()
    stop_event: threading.Event | None = None
    soft_event: threading.Event | None = None
    acq_thread: threading.Thread | None = None
    acq_started = 0.0

    def send(msg: dict) -> None:
        _send_ctrl_msg(conn, lock, msg)

    try:
        buf = ''
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk.decode('utf-8')
            while '\n' in buf:
                line, buf = buf.split('\n', 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                cmd = msg.get('cmd')
                if cmd == 'start':
                    if acq_thread and acq_thread.is_alive():
                        age = time.time() - acq_started
                        send({'status': 'busy'})
                        send({'status': 'log',
                              'msg': f'START refused: {acq_thread.name} still '
                                     f'alive after {age:.1f} s '
                                     f'(stop_event set={stop_event.is_set()})\n'})
                        continue
                    stop_event  = threading.Event()
                    soft_event  = threading.Event()
                    acq_started = time.time()
                    acq_thread = threading.Thread(
                        target=_run_acquisition_cmd,
                        args=(msg, stop_event, send, status_fn, soft_event),
                        daemon=True,
                    )
                    acq_thread.start()
                elif cmd == 'intensity':
                    if acq_thread and acq_thread.is_alive():
                        age = time.time() - acq_started
                        send({'status': 'busy'})
                        send({'status': 'log',
                              'msg': f'INTENSITY refused: {acq_thread.name} still '
                                     f'alive after {age:.1f} s '
                                     f'(stop_event set={stop_event.is_set()})\n'})
                        continue
                    stop_event  = threading.Event()
                    soft_event  = threading.Event()
                    acq_started = time.time()
                    acq_thread = threading.Thread(
                        target=_run_intensity_cmd,
                        args=(msg, send, status_fn),
                        daemon=True,
                    )
                    acq_thread.start()
                elif cmd == 'stop' and msg.get('mode') == 'soft':
                    # Drain everything lSPAD has buffered; discard nothing.
                    if stop_event is not None:
                        soft_event.set()
                        stop_event.set()
                elif cmd == 'abort':
                    # Also the escalation path: clearing soft_event mid-drain
                    # tells run() to stop waiting and drop the remainder.
                    if soft_event is not None:
                        soft_event.clear()
                    if stop_event is not None:
                        stop_event.set()
    except OSError:
        pass
    finally:
        if stop_event is not None:
            stop_event.set()
        conn.close()


def _run_acquisition_cmd(params: dict, stop_event: threading.Event,
                          send_ctrl, status_fn,
                          soft_event: threading.Event | None = None) -> None:
    try:
        recv_host  = params['recv_host']
        recv_port  = int(params['recv_port'])
        output_dir = params['output_dir']
        duration   = float(params['duration'])
        test_mode  = bool(params.get('test', False))

        send_ctrl({'status': 'connecting'})
        sock = connect_receiver(recv_host, recv_port)
        send_ctrl({'status': 'streaming'})
        status_fn({'event': 'streaming'})

        stats = None
        try:
            stats = run(sock, output_dir, duration, test_mode, stop_event,
                        log_fn=lambda msg: send_ctrl({'status': 'log', 'msg': msg}),
                        soft_event=soft_event)
        finally:
            sock.close()

        send_ctrl({'status': 'done', 'stats': stats or {}})
    except Exception as exc:
        send_ctrl({'status': 'error', 'msg': f'{type(exc).__name__}: {exc}'})
        send_ctrl({'status': 'log',
                   'msg': f'acquisition traceback:\n{traceback.format_exc()}\n'})


def _run_intensity_cmd(params: dict, send_ctrl, status_fn) -> None:
    try:
        recv_host  = params['recv_host']
        recv_port  = int(params['recv_port'])
        output_dir = params['output_dir']
        duration   = float(params['duration'])

        send_ctrl({'status': 'connecting'})
        sock = connect_receiver(recv_host, recv_port)
        send_ctrl({'status': 'measuring'})
        status_fn({'event': 'measuring'})

        n_lines = 0
        try:
            n_lines = run_intensity(
                sock, output_dir, duration,
                log_fn=lambda msg: send_ctrl({'status': 'log', 'msg': msg}))
        finally:
            sock.close()

        send_ctrl({'status': 'intensity_done', 'lines': n_lines})
    except Exception as exc:
        send_ctrl({'status': 'error', 'msg': f'{type(exc).__name__}: {exc}'})
        send_ctrl({'status': 'log',
                   'msg': f'intensity measurement traceback:\n{traceback.format_exc()}\n'})
    finally:
        status_fn({'event': 'idle'})


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SPAD sender')
    parser.add_argument('--test', action='store_true',
                        help='Stream fake data without connecting to the detector')
    parser.add_argument('--target-host', default=TARGET_HOST,
                        help=f'Receiver PC IP (default: {TARGET_HOST})')
    parser.add_argument('--target-port', type=int, default=TARGET_PORT,
                        help=f'Receiver PC port (default: {TARGET_PORT})')
    parser.add_argument('--duration', type=float, default=DURATION_S,
                        help=f'Acquisition duration in seconds (default: {DURATION_S})')
    parser.add_argument('--output-dir', default='./spad_data',
                        help='Output folder on the receiver PC (default: ./spad_data)')
    args = parser.parse_args()

    print(f'Connecting to {args.target_host}:{args.target_port} ...')
    sock = connect_receiver(args.target_host, args.target_port)
    print('Connected.')

    stop = threading.Event()
    try:
        run(sock, args.output_dir, args.duration, args.test, stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        sock.close()
