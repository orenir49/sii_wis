"""End-to-end validation for the live A/B/C wire-encoding confirmation
(docs/raw_timestamp_wire_encoding_bakeoff.md): replay a real capture pair
through the REAL wire protocol -- node_backend.run() -> master_backend.
run_session_loop -- once per wire_mode, feed a real ChannelGraph + PairPool,
and assert the g2 histogram for one shared pixel is bit-identical across
baseline/raw/delta.

This is the load-bearing gate before touching live hardware: unlike the
synthetic-stream test in tests/test_channel_graph.py, this exercises the
actual sockets, KEY_SETUP/KEY_SETUP_V2 negotiation, and Phase 1 bucketing on
real detector bytes end to end. Switching wire representation must never
change the physics answer -- any difference here is a codec or wiring bug,
full stop, and must be fixed before a live run.

    python tools\\validate_wire_mode_e2e.py
    python tools\\validate_wire_mode_e2e.py --capture1 <path> --capture2 <path> --loc 291
"""
import argparse
import os
import socket
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import raw_dump
import replay as replay_mod
import node_backend
import master_backend
import pair_map
import wire_format
from correlate_engine import ChannelGraph
from correlate_kernel import PairPool, prewarm

DEFAULT_CAP1 = os.path.join(ROOT, 'spad_data', 'captures', '26-8-26_40px', 'cap_node1.raw')
DEFAULT_CAP2 = os.path.join(ROOT, 'spad_data', 'captures', '26-8-26_40px', 'cap_node2.raw')
TMAX = 500_000.0
BIN_WIDTH = 5_000.0
NBINS = int(2 * TMAX / BIN_WIDTH)
N_SHIFT = 20


def _encode_sentinel(ts: np.ndarray, wire_mode: str) -> bytes:
    if wire_mode == 'delta':
        return wire_format.encode_deltas(ts)
    if wire_mode == 'raw':
        r, c, f = wire_format.split_int64(ts, node_backend.PS_PER_COUNT, node_backend.COUNTS_PER_RESET)
        return wire_format.pack_raw_columns(r, c, f)
    return ts.tobytes()


def run_one_node(cap_path, wire_mode, graph, node) -> None:
    """Replay cap_path through the real node_backend.run() ->
    master_backend.run_session_loop pipeline, hooked into `graph`'s real
    hooks_node1/hooks_node2 queues -- exactly how MultiCorrelateWindow does
    it in production, minus the Tk window."""
    chunks = list(raw_dump.read_chunks(cap_path))
    rsock = replay_mod.ReplaySocket(chunks)
    real_open = node_backend.open_lspad_stream
    node_backend.open_lspad_stream = lambda *a, **k: rsock

    hooks = graph.hooks_node1 if node == 1 else graph.hooks_node2
    srv, cli = socket.socketpair()
    done = threading.Event()
    observed = []

    def receiver():
        try:
            master_backend.run_session_loop(
                conn=srv, log_fn=lambda *_: None, pixel_hooks=hooks,
                write_hooked=False, on_wire_mode=observed.append)
        finally:
            done.set()

    th = threading.Thread(target=receiver, daemon=True)
    th.start()
    try:
        stop = threading.Event()
        node_backend.run(sock=cli, output_dir='(unused)', duration=1.0,
                         test_mode=False, stop_event=stop, log_fn=lambda *_: None,
                         wire_mode=wire_mode)
    finally:
        node_backend.open_lspad_stream = real_open
        rsock.close()
        cli.close()
        done.wait(timeout=30)
        srv.close()
    assert observed == [wire_mode], f'node{node}: observed {observed}, expected [{wire_mode!r}]'


def run_mode(cap1, cap2, loc, wire_mode):
    pl = pair_map.derive('identity', lo=loc, hi=loc)
    g = ChannelGraph(pl, TMAX, offset=0, wire_mode=wire_mode)
    g.start()

    t1 = threading.Thread(target=run_one_node, args=(cap1, wire_mode, g, 1))
    t2 = threading.Thread(target=run_one_node, args=(cap2, wire_mode, g, 2))
    t1.start(); t2.start()
    t1.join(); t2.join()

    g.drain_all()
    releases = [g.release()]
    # Flush the tail with a far-future sentinel so node 1's remainder releases.
    top = max((c.last_ts or 0) for c in g.channels) + 10 ** 15
    sentinel = _encode_sentinel(np.array([top], dtype=np.int64), wire_mode)
    for p in g.ch2:
        g.ch2[p].q.put(sentinel)
    g.drain_all()
    releases.append(g.release())

    pool = PairPool()
    hist_total = np.zeros(NBINS, dtype=np.int64)
    n1 = 0
    for rel in releases:
        if not rel.batches:
            continue
        batches = [((p1, p2), t1b, t2a) for p1, p2, t1b, t2a in rel.batches]
        for h in pool.run(batches, BIN_WIDTH, TMAX, NBINS, N_SHIFT).values():
            hist_total += h
        n1 += sum(len(t1b) for _, _, t1b, _ in rel.batches)
    return hist_total, n1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--capture1', default=DEFAULT_CAP1)
    ap.add_argument('--capture2', default=DEFAULT_CAP2)
    ap.add_argument('--loc', type=int, default=291,
                    help='physical pixel location active on both captures (default: 291)')
    args = ap.parse_args()

    prewarm()
    results, counts = {}, {}
    for mode in ('baseline', 'raw', 'delta'):
        hist, n1 = run_mode(args.capture1, args.capture2, args.loc, mode)
        results[mode], counts[mode] = hist, n1
        print(f'{mode}: {n1} node-1 events released, hist sum={hist.sum()}, peak bin={hist.max()}')

    ok_raw = np.array_equal(results['raw'], results['baseline'])
    ok_delta = np.array_equal(results['delta'], results['baseline'])
    print(f'raw == baseline (bit-identical): {ok_raw}')
    print(f'delta == baseline (bit-identical): {ok_delta}')
    if not (ok_raw and ok_delta):
        print('raw diff bins:', np.nonzero(results['raw'] != results['baseline'])[0][:10])
        print('delta diff bins:', np.nonzero(results['delta'] != results['baseline'])[0][:10])
        return 1
    print('PASS: all three wire modes produced bit-identical g2 histograms')
    return 0


if __name__ == '__main__':
    sys.exit(main())
