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
import polars as pl
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

PRESTART_DRAIN_S  = 120.0   # budget for reading a stale backlog to silence;
                            # ~120 MB/s on loopback, so this covers ~14 GB
TDC_CALIB_S       = 180.0   # T,c,1 runs for minutes

# T-mode acquisition (docs/lspad_streaming_throttle.md,
# docs/tmode_architecture_feasibility.md): lSPAD's `T,<ms>` file-based mode
# replaces `SB,<ms>` streaming on this branch -- see run()'s T-mode ingestion
# loop. Run-folder numbering increments once per T, command for the lifetime
# of the lSPAD.exe process (verified empirically, 2026-09-06) and resets on
# lSPAD restart, so the folder is discovered by diffing the directory listing
# before/after sending T,, never guessed from a counter.
#
# lSPAD's own default save location is under its install directory
# (C:\Program Files (x86)\SPADlambda\...); a node user may not have write
# access there. `D,<dir>` (LSPAD_CLI.md) redirects it, so open_lspad_tmode_
# stream() points it at this repo's own gitignored spad_data/ instead, where
# the node always has write access. Verified live against real lSPAD
# (2026-09-06): it appends its fixed "data/tdc/RunNNN/" suffix to whatever
# `D,<dir>` was sent via a plain string concatenation with NO separator
# inserted -- a `dir` with no trailing separator produced the nonsense path
# "...spad_data\tdcdata/tdc/RunNNN/". TMODE_SAVE_DIR must end in one.
TMODE_SAVE_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'spad_data') + os.sep
TMODE_RUN_ROOT   = os.path.join(TMODE_SAVE_DIR, 'data', 'tdc')
TMODE_RUN_WAIT_S = 10.0    # new Run folder must appear within this long
TMODE_POLL_S     = 0.05    # file-lane poll interval while a file might still be arriving

# ---------------------------------------------------------------------------
# Physics
# ---------------------------------------------------------------------------
PS_PER_COUNT     = int((1 / 10e6) * 1e12)
COUNTS_PER_RESET = 2**16

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
FILE_START_ID     = 239      # lSPAD file/stream-start marker -- expected once per session
KNOWN_MARKER_IDS  = np.array(sorted(SPECIAL) + [RESET_ID, OVERFLOW_ID])

# Traffic a healthy stream is *made of*: photons, the coarse-counter reset, the
# dwell/line/frame sync markers, and the file-start marker. Every other id is
# abnormal and is reported live by report_abnormal() — including OVERFLOW_ID,
# which is "known" only in the sense that we know what it means.
NORMAL_MARKER_IDS = np.array(sorted(SPECIAL) + [RESET_ID, FILE_START_ID])
MARKER_NAMES = {
    OVERFLOW_ID:   'FIFO overflow, photons already lost',
}
ANOM_LOG_S     = 2.0   # min seconds between rollup lines for one (chip, id)
ANOM_MAX_FIRST = 40    # cap on distinct first-sighting lines per session

LAG_CHECK_S = 5.0      # how often to recompute parser lag
LAG_WARN_S  = 2.0      # lag above this means data is queueing up

master_loc = np.array([PIXMAP[170 + i] for i in range(150)])
slave_loc  = np.array([PIXMAP[i]       for i in range(170)])

# ---------------------------------------------------------------------------
# Fused (chip, pixel_nr) slot table -- Stage 2a bucketing fix.
#
# Replaces the old per-chip "for uid in np.unique(phys_pid): bufs[...].append(
# phys_ts[phys_pid == uid])" loop, whose cost is one numpy boolean-mask pass
# per *active pixel* regardless of chunk length (O(chunk x N_active)) — the
# team's own measurement found this loop, not the PIXMAP lookup or the
# ps-scale combine arithmetic, is the actual bottleneck at N active pixels.
#
# pixel_nr is a raw byte (0-255) parsed straight off the wire, so it and
# is_mast fuse losslessly into one uint16 slot: pixel_nr | (is_mast << 8) --
# 0-255 for the slave chip, 256-511 for the master chip. SLOT_DEST maps each
# of the 512 possible slots to a compact destination index (or -1 to
# discard); DEST_KEYS maps that index back to the bufs key. Grouping a
# chunk's events by destination is then one stable argsort + one bincount,
# not one pass per pixel.
#
# Built once at import time from master_loc/slave_loc/SPECIAL — the same
# three inputs the old loop closed over — so it is exactly as correct as
# they are, including the master 150-169 hole: master_loc only has 150
# entries (master pixel ids 0-149), so slots 406-425 (256+150 .. 256+169)
# are never assigned and stay -1, discarded exactly as the old loop's
# `n_phys=150` bound discarded them.
#
# Only physical photons and the dwell/line/frame sync markers are assigned a
# destination here. Everything else this parse loop cares about — the reset
# marker (234), FIFO overflow (247), file-start (239), and any abnormal/
# unknown id — is read straight off the original pixel_nr/is_mast arrays
# elsewhere in the loop (reset cumsum, overflow count, report_abnormal) and
# was never part of this grouping loop, so it is correctly discarded (-1)
# here too.
N_SLOTS = 512


def _build_slot_table():
    dest_keys: list = []
    slot_dest = np.full(N_SLOTS, -1, dtype=np.int32)

    def add(slot: int, key) -> None:
        slot_dest[slot] = len(dest_keys)
        dest_keys.append(key)

    for uid, loc in enumerate(slave_loc):
        add(uid, int(loc))
    for uid, loc in enumerate(master_loc):
        add(256 + uid, int(loc))
    n_phys_dest = len(dest_keys)

    for sp_id, name in SPECIAL.items():
        add(sp_id, ('slave', name))
        add(256 + sp_id, ('master', name))

    return slot_dest, dest_keys, n_phys_dest


SLOT_DEST, DEST_KEYS, N_PHYS_DEST = _build_slot_table()
N_DEST = len(DEST_KEYS)
# The master 150-169 hole (valid pixel ids on the slave chip, not on master)
# must survive the table build: those slots stay discarded (-1), not aliased
# onto some other destination.
assert (SLOT_DEST[256 + 150:256 + 170] == -1).all()

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


# ---------------------------------------------------------------------------
# T-mode file ingestion (docs/tmode_architecture_feasibility.md)
# ---------------------------------------------------------------------------

def _tmode_file_path(run_dir: str, chip: str, idx: int) -> str:
    return os.path.join(run_dir, f'data_{chip}{idx:03d}.txt')


def read_tmode_file(path: str, skip_first_line: bool) -> tuple:
    """Parse one data_{master,slave}NNN.txt into (pixel, coarse, fine) int
    arrays, file order preserved.

    Every row is a uniform 3-field `pixel,coarse,fine` CSV -- including
    RESET_ID rows (`234,0,<seq>`) -- except the very first row of the very
    first file of a run (`239,<coarse>`, the file-start marker, 2 fields),
    which the caller must skip explicitly (`skip_first_line`) since polars
    needs a uniform column count. Verified against real captures
    (2026-09-06): no other marker id (dwell/line/frame/overflow) has been
    observed in this format yet, so they are not specifically excluded here
    -- run()'s abnormal-marker reporting downstream still catches them by
    physical-pixel-range the same way it does for the SB stream.
    """
    df = pl.read_csv(path, has_header=False, skip_rows=1 if skip_first_line else 0,
                      new_columns=['pixel', 'coarse', 'fine'],
                      schema_overrides={'pixel': pl.Int32, 'coarse': pl.Int32,
                                        'fine': pl.Int64})
    return (df['pixel'].to_numpy(), df['coarse'].to_numpy(), df['fine'].to_numpy())


def reconstruct_tmode_epochs(pixel: np.ndarray, coarse: np.ndarray, fine: np.ndarray,
                              epoch_offset: int) -> tuple:
    """Reconstruct absolute ps timestamps from one T-mode file's columns
    (one chip, file order). Returns (time_ps, pixel) for photon rows only
    (RESET_ID rows dropped) plus the epoch_offset to carry into the next
    file of this chip.

    T-mode's reset marker (`234,0,<seq>`) is authoritative and needs no
    marker-ordering special case (the retired SB stream's own
    correct_boundary_epochs() existed only to fix an ordering defect this
    format doesn't appear to have): verified against a real capture that
    every genuine epoch wrap is marked by exactly one RESET_ID row (54/54
    matched), and that `<seq>` is a continuous count across files, not reset
    per file (file000's last marker `234,0,53`, file001's first
    `234,0,54`). So epoch tracking here is a plain inclusive cumsum of
    RESET_ID rows, offset by what was carried in from earlier files.

    The carried epoch_offset is cross-checked against the markers' own
    `<seq>` values (stored in the `fine` column for those rows) rather than
    trusted alone: a silent mismatch would misplace a whole file's
    timestamps by some multiple of 6.5536 ms with no other visible symptom.
    """
    is_reset = pixel == RESET_ID
    if is_reset.any():
        seq = fine[is_reset]
        expected_seq = epoch_offset + np.arange(len(seq))
        if not np.array_equal(seq, expected_seq):
            raise ValueError(
                f'T-mode epoch continuity mismatch: expected reset seq '
                f'starting at {epoch_offset}, got {seq[:5].tolist()} '
                f'(first 5 of {len(seq)})')
    epoch = epoch_offset + np.cumsum(is_reset)
    time_ps = ((epoch.astype(np.int64) * COUNTS_PER_RESET + coarse.astype(np.int64))
               * PS_PER_COUNT + fine.astype(np.int64))
    next_offset = epoch_offset + int(is_reset.sum())
    keep = ~is_reset
    return time_ps[keep], pixel[keep], next_offset


def find_tmode_run_dir(before: set, log_fn=print,
                       wait_s: float = TMODE_RUN_WAIT_S) -> str:
    """Poll TMODE_RUN_ROOT for a Run folder not in `before` (a snapshot taken
    just before sending T,). Diffing the listing rather than tracking/
    guessing the run number is deliberate: the counter increments per T,
    command for the life of the lSPAD.exe process and resets on restart
    (verified empirically), so guessing it would silently break the first
    time something else touches this lSPAD."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            current = set(os.listdir(TMODE_RUN_ROOT))
        except OSError:
            current = set()
        new = current - before
        if new:
            return os.path.join(TMODE_RUN_ROOT, sorted(new)[0])
        time.sleep(TMODE_POLL_S)
    raise RuntimeError(
        f'No new Run folder appeared under {TMODE_RUN_ROOT} within '
        f'{wait_s:.0f} s of sending T, -- lSPAD may not have accepted the '
        f'D,<dir> save-path command (see open_lspad_tmode_stream), or is '
        f'not running on this machine.')


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


def open_lspad_tmode_stream(duration: float, log_fn=print) -> tuple:
    """Connect to lSPAD, clear any leftover acquisition, point its save path
    at this repo's spad_data/ (TMODE_SAVE_DIR), check the TDC calibration,
    and start a T-mode acquisition. Returns (spad_sock, run_dir) -- run_dir
    is the newly-created Run folder this session's files land in, discovered
    by diffing TMODE_RUN_ROOT's listing from just before the T, command was
    sent (see find_tmode_run_dir()).

    Same STOP-drain-then-calibrate handshake as the retired SB path (see
    docs/lspad_streaming_throttle.md, docs/raw_timestamp_wire_encoding_
    bakeoff.md for why that path no longer exists on this branch) -- only
    the final command differs.
    """
    spad_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    spad_sock.settimeout(LSPAD_HANDSHAKE_S)
    spad_sock.connect((SPAD_HOST, SPAD_PORT))
    # Clear any acquisition still running from a previous session before
    # touching the command protocol. lSPAD streams to every connected
    # client, so a leftover SB/S would be read as our command replies and
    # would desynchronise the handshake. Sending STOP straight away lets one
    # drain cover the banner, any leftover stream and the STOP reply — three
    # waits cost >1 s of the sparse-cal window.
    spad_sock.sendall(b'STOP\n')
    t_pre = time.time()
    _, pre_n = drain_lspad(spad_sock, quiet_for=0.4, cap=PRESTART_DRAIN_S)
    if pre_n > 256:
        dt = time.time() - t_pre
        log_fn(f'pre-START STOP: discarded {pre_n / 1e6:,.0f} MB of '
               f'leftover stream in {dt:.1f} s before lSPAD went quiet\n')

    # lSPAD requires the directory to already exist (an unrecognised path
    # replies 'Incorrect path' -- verified live) and echoes the given path
    # back verbatim on success.
    os.makedirs(TMODE_SAVE_DIR, exist_ok=True)
    spad_sock.sendall(f'D,{TMODE_SAVE_DIR}\n'.encode('utf8'))
    d_reply, _ = drain_lspad(spad_sock, quiet_for=0.2, cap=LSPAD_HANDSHAKE_S)
    d_reply_text = d_reply.decode('utf8', errors='replace').strip()
    if d_reply_text != TMODE_SAVE_DIR:
        raise RuntimeError(
            f'lSPAD rejected the save-path command D,{TMODE_SAVE_DIR} '
            f'(replied {d_reply_text!r}) -- refusing to start, since this '
            f"session's Run folder would land somewhere neither this code "
            f'nor the master is looking.')

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

    before = (set(os.listdir(TMODE_RUN_ROOT))
             if os.path.isdir(TMODE_RUN_ROOT) else set())

    spad_sock.settimeout(None)   # main loop drives its own select()
    spad_sock.sendall(f'T,{int(duration * 1000)}\n'.encode('utf8'))

    run_dir = find_tmode_run_dir(before, log_fn)
    log_fn(f'T-mode acquisition started, output: {run_dir}\n')
    return spad_sock, run_dir


def run(sock: socket.socket,
        output_dir: str,
        duration: float,
        test_mode: bool,
        stop_event: threading.Event,
        log_fn=print,
        soft_event: threading.Event | None = None) -> dict:
    """
    Run one acquisition session over an already-connected socket.
    Sends KEY_SETUP, streams data chunks, then sends KEY_END. Does NOT close
    the socket — the caller owns it.

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

        Frames are simply concatenated before the write, which the receiver
        cannot tell apart from separate writes. One queue item
        and one sendall per flush instead of one per pixel: at 320 active
        pixels that collapses 326 syscalls into 1. The old behaviour issued
        ~265k sendall/s at full rate and overfilled the 200-slot queue on a
        single flush, blocking the parser mid-flush — which stopped it
        reading the socket and pushed the loss into lSPAD's FIFO.

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
            # lSPAD's own TCP command protocol — see LSPAD_CLI.md for the full
            # command set. T-mode (docs/lspad_streaming_throttle.md,
            # docs/tmode_architecture_feasibility.md) replaces SB streaming on
            # this branch: lSPAD writes rotating data_{master,slave}NNN.txt
            # files instead of pushing bytes down this socket, and this loop's
            # job is to notice, read and bucket each one as it completes.
            spad_sock, run_dir = open_lspad_tmode_stream(duration, log_fn)

            m_next, s_next   = 0, 0        # next file index due, per chip
            m_epoch, s_epoch = 0, 0        # carried reset-cumsum offset, per chip
            m_done,  s_done  = False, False
            n_files          = 0
            total_bytes      = 0
            reply_buf        = b''
            reply_received   = False
            t_stream         = time.time()
            last_lag_check   = t_stream
            stopping         = False

            def _try_lane(chip: str, is_mast: bool, next_idx: int, epoch_offset: int) -> tuple:
                """Try to advance one file lane by one file. Returns
                (next_idx, epoch_offset, lane_done, progressed, n_bytes,
                n_photon_events, dwell_seen). A file is read once it's
                confirmed finalized (the next file already exists, or the
                whole acquisition is over) AND its size has been stable
                across one poll interval — defensive against reading a file
                lSPAD might still be appending to (not race-tested against
                real hardware; see docs/tmode_architecture_feasibility.md)."""
                path = _tmode_file_path(run_dir, chip, next_idx)
                try:
                    size1 = os.path.getsize(path)
                except OSError:
                    return next_idx, epoch_offset, reply_received, False, 0, 0, False
                next_path = _tmode_file_path(run_dir, chip, next_idx + 1)
                if not os.path.exists(next_path) and not reply_received:
                    return next_idx, epoch_offset, False, False, 0, 0, False
                time.sleep(TMODE_POLL_S)
                try:
                    size2 = os.path.getsize(path)
                except OSError:
                    size2 = -1
                if size1 != size2:
                    return next_idx, epoch_offset, False, False, 0, 0, False

                pixel, coarse, fine = read_tmode_file(path, skip_first_line=(next_idx == 0))
                time_ps, pixel_nr, epoch_offset = reconstruct_tmode_epochs(
                    pixel, coarse, fine, epoch_offset)

                stats['records'] += len(pixel)
                n_overflow = int(np.sum(pixel_nr == OVERFLOW_ID))
                if n_overflow:
                    stats['overflow'] += n_overflow

                if time_ps.size:
                    if stats['first_ts'] is None:
                        stats['first_ts'] = int(time_ps[0])
                    stats['last_ts'] = int(time_ps[-1])

                # Anything that is neither a physical pixel for this chip nor a
                # normal marker is discarded by the bucketing below. Report it
                # live, same as the retired SB path did.
                phys_ok  = pixel_nr < (150 if is_mast else 170)
                abnormal = ~(phys_ok | np.isin(pixel_nr, NORMAL_MARKER_IDS))
                if abnormal.any():
                    stats['unknown'] += int(
                        (abnormal & ~np.isin(pixel_nr, KNOWN_MARKER_IDS)).sum())
                    is_mast_arr = np.full(pixel_nr.shape, is_mast, dtype=bool)
                    report_abnormal(abnormal, pixel_nr, is_mast_arr, time_ps,
                                    stats['records'] - len(pixel))

                # Same fused-key bucketing pass the retired SB path used —
                # is_mast is one value for the whole file here rather than a
                # per-record array, which broadcasts into the slot formula
                # unchanged.
                slot = pixel_nr.astype(np.uint16) | (np.uint16(1 if is_mast else 0) << 8)
                dest = SLOT_DEST[slot]
                keep = dest >= 0
                dwell_seen = False
                n_events = 0
                if keep.any():
                    dest_k = dest[keep]
                    order  = np.argsort(dest_k, kind='stable')
                    ts_sorted = time_ps[keep][order]
                    counts = np.bincount(dest_k, minlength=N_DEST)
                    bounds = np.concatenate(([0], np.cumsum(counts)))
                    n_events = int(counts[:N_PHYS_DEST].sum())
                    for d in np.nonzero(counts)[0]:
                        key = DEST_KEYS[d]
                        bufs[key].append(ts_sorted[bounds[d]:bounds[d + 1]])
                        if isinstance(key, tuple) and key[1] == 'dwell':
                            dwell_seen = True

                return next_idx + 1, epoch_offset, False, True, size1, n_events, dwell_seen

            try:
                while True:
                    # T-mode finalizes quickly once STOP lands (~1 s in
                    # practice, verified 2026-09-06) — there is no multi-
                    # minute in-flight backlog to drain or discard the way
                    # SB's own buffer could hold, so soft stop and abort
                    # converge to the same behaviour here: send STOP, then
                    # keep reading whatever files complete until this loop's
                    # own done/reply conditions are met below.
                    if stop_event.is_set() and not stopping:
                        stopping = True
                        stats['stop_mode'] = 'soft' if is_soft() else 'abort'
                        log_fn('Stopping T-mode acquisition — sending STOP to '
                               'lSPAD, then reading whatever files it finishes '
                               'writing.\n')
                        try:
                            spad_sock.sendall(b'STOP\n')
                        except OSError as exc:
                            log_fn(f'STOP failed: {exc!r}\n')

                    # Non-blocking check for the T, command's own reply — it
                    # only arrives once the whole acquisition (every file,
                    # fully finalized) is done, verified 2026-09-06.
                    if not reply_received:
                        r, _, _ = select.select([spad_sock], [], [], 0)
                        if r:
                            chunk = spad_sock.recv(4096)
                            if not chunk:
                                reply_received = True
                            else:
                                reply_buf += chunk
                                if b'ERROR' in reply_buf:
                                    log_fn(f'lSPAD ERROR reply: {reply_buf!r}\n')
                                    reply_received = True
                                elif b'Data saved' in reply_buf:
                                    reply_received = True

                    progressed = False
                    dwell_seen_any = False
                    if not m_done:
                        m_next, m_epoch, m_done, prog, nb, ne, dwell = _try_lane(
                            'master', True, m_next, m_epoch)
                        if prog:
                            total_bytes += nb
                            n_files += 1
                            events_since_flush += ne
                            progressed = True
                            dwell_seen_any |= dwell
                    if not s_done:
                        s_next, s_epoch, s_done, prog, nb, ne, dwell = _try_lane(
                            'slave', False, s_next, s_epoch)
                        if prog:
                            total_bytes += nb
                            n_files += 1
                            events_since_flush += ne
                            progressed = True
                            dwell_seen_any |= dwell

                    # Markers go out immediately — calibration needs them
                    # promptly. Pixel buffers flush on a size OR time bound,
                    # same as the retired SB path.
                    if dwell_seen_any:
                        flush(MARKER_BUF_KEYS)
                    now = time.time()
                    if (events_since_flush >= FLUSH_EVERY
                            or now - last_flush >= FLUSH_INTERVAL_S):
                        flush()
                        events_since_flush = 0
                        last_flush = now

                    if time.time() - last_lag_check >= LAG_CHECK_S:
                        last_lag_check = time.time()
                        if stats['first_ts'] is not None:
                            lag = ((last_lag_check - t_stream)
                                   - (stats['last_ts'] - stats['first_ts']) / 1e12)
                            stats['lag_s'] = round(lag, 2)
                            if lag > stats['lag_max_s']:
                                stats['lag_max_s'] = round(lag, 2)
                            if lag > LAG_WARN_S and not stopping:
                                log_fn(f'WARNING: file ingestion is {lag:.1f} s '
                                       f'behind the detector — data is queueing\n')

                    if m_done and s_done and reply_received:
                        break
                    if not progressed:
                        time.sleep(TMODE_POLL_S)

                stats['recv_calls'] = n_files   # repurposed for T-mode: files read, not recv() calls
            finally:
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
