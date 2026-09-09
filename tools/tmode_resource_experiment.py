"""One-shot experiment: launch both nodes, run the RAM/disk resource monitor
across a whole window, and drive one real T-mode timestamp acquisition
through the middle of it -- to see, resource-by-resource (available RAM,
paging rate, disk queue/throughput, lSPAD's own working set/IO), which one
actually moves when a live acquisition is running versus sitting idle before
and after it.

Talks the same control/data protocol master.py's NodePanel does
(send_start-style JSON, run_session_loop) but headlessly -- no Tk, no GUI.
Leaves lSPAD.exe and node.py running on both nodes afterward, same as a
normal master.py session; this script only tears down its own local sockets
and threads.

Usage:
    python tools/tmode_resource_experiment.py --mask mask_sweep_40 \
        --monitor-duration 120 --acq-duration 30 --outdir figs/9-9-26/data
"""
import argparse
import json
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import ssh_launcher
from master_backend import start_server, run_session_loop
import monitor_node_resources as mnr

NODES = {
    1: {'host': '192.168.1.11', 'user': 'labcomp1', 'cmd_port': 50010, 'data_port': 50007},
    2: {'host': '192.168.2.11', 'user': 'oreni',    'cmd_port': 50010, 'data_port': 50008},
}
BASELINE_S = 15.0          # resource-monitor lead-in before the acquisition starts
CTRL_CONNECT_RETRIES = 10  # node.py's command server needs a moment to bind after launch
CTRL_CONNECT_DELAY_S = 1.5


def _log(node_id, msg, log):
    log(f'[node{node_id}] {msg}\n')


def launch_one(node_id: int, mask_name: str, log) -> None:
    host, user = NODES[node_id]['host'], NODES[node_id]['user']
    ssh_launcher.launch_node(host, user, f'{mask_name}.txt',
                             lambda m: _log(node_id, m.rstrip('\n'), log))


def launch_both(mask_name: str, log) -> None:
    errors = {}

    def _run(node_id):
        try:
            launch_one(node_id, mask_name, log)
        except Exception as exc:
            errors[node_id] = exc

    threads = [threading.Thread(target=_run, args=(n,), daemon=True) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise RuntimeError(f'launch failed: {errors}')


def _connect_ctrl(host: str, cmd_port: int) -> socket.socket:
    """Connect to node.py's command server, retrying briefly -- launch_node
    returns as soon as node.py is *started*, not once its command server is
    actually listening."""
    last_exc = None
    for attempt in range(CTRL_CONNECT_RETRIES):
        try:
            ctrl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ctrl.settimeout(5.0)
            ctrl.connect((host, cmd_port))
            ctrl.settimeout(None)
            ctrl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return ctrl
        except OSError as exc:
            last_exc = exc
            time.sleep(CTRL_CONNECT_DELAY_S)
    raise RuntimeError(f'could not connect to {host}:{cmd_port} after '
                       f'{CTRL_CONNECT_RETRIES} attempts: {last_exc!r}')


def run_acquisition(node_id: int, duration_s: float, log, done_timeout_s: float = None) -> dict:
    """Mirrors NodePanel._connect + send_start + _accept_data_thread, headless.
    Blocks until the sender reports 'done'/'error' or done_timeout_s elapses.
    Returns a record with the sender's own stats dict and wall-clock timing."""
    if done_timeout_s is None:
        done_timeout_s = max(90.0, duration_s * 3.0)
    host = NODES[node_id]['host']
    cmd_port = NODES[node_id]['cmd_port']
    data_port = NODES[node_id]['data_port']

    data_server = start_server(data_port)
    ctrl = _connect_ctrl(host, cmd_port)

    result = {'status': None, 'stats': {}}
    result_evt = threading.Event()

    def ctrl_reader():
        buf = ''
        try:
            while True:
                chunk = ctrl.recv(4096)
                if not chunk:
                    break
                buf += chunk.decode('utf-8', 'replace')
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    s = msg.get('status')
                    if s == 'log':
                        _log(node_id, msg.get('msg', '').rstrip('\n'), log)
                    elif s == 'progress':
                        _log(node_id, f'progress m{msg.get("m_idx")}/{msg.get("m_total")} '
                                      f's{msg.get("s_idx")}/{msg.get("s_total")}', log)
                    elif s == 'error':
                        result['status'] = 'error'
                        result['msg'] = msg.get('msg', '')
                        result_evt.set()
                    elif s == 'done':
                        result['status'] = 'done'
                        result['stats'] = msg.get('stats', {})
                        result_evt.set()
        except OSError:
            pass

    def accept_thread():
        try:
            conn, addr = data_server.accept()
        except OSError:
            return
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _log(node_id, f'data connection from {addr[0]}', log)
        try:
            run_session_loop(
                conn,
                log_fn=lambda m: _log(node_id, m.rstrip('\n'), log),
                write_hooked=True,   # 'Save timestamps to disk' -- write_mode='timestamps'
            )
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    threading.Thread(target=ctrl_reader, daemon=True).start()
    threading.Thread(target=accept_thread, daemon=True).start()

    recv_host = ctrl.getsockname()[0]
    output_dir = f'./spad_data/node{node_id}'
    start_msg = {
        'cmd': 'start', 'recv_host': recv_host, 'recv_port': data_port,
        'output_dir': output_dir, 'duration': duration_s, 'test': False,
    }
    t_start = time.time()
    ctrl.sendall((json.dumps(start_msg) + '\n').encode())
    _log(node_id, f'sent start (duration={duration_s:.0f}s)', log)

    if not result_evt.wait(timeout=done_timeout_s):
        _log(node_id, f'WARNING: no done/error within {done_timeout_s:.0f}s', log)
    t_end = time.time()

    try:
        ctrl.close()
    except OSError:
        pass
    try:
        data_server.close()
    except OSError:
        pass

    return {
        'node': node_id, 'status': result['status'], 'stats': result.get('stats', {}),
        't_start': t_start, 't_end': t_end, 'error_msg': result.get('msg'),
    }


def run_experiment(mask_name: str, monitor_duration: float, acq_duration: float,
                   outdir: str, log=print) -> tuple:
    os.makedirs(outdir, exist_ok=True)

    log(f'=== launching both nodes with {mask_name}.txt ===\n')
    launch_both(mask_name, log)

    log(f'=== starting {monitor_duration:.0f}s resource monitor on both nodes ===\n')
    monitor_results = {}

    def _monitor(node_id):
        monitor_results[node_id] = mnr.sample_node(node_id, monitor_duration, log=log)

    monitor_threads = [threading.Thread(target=_monitor, args=(n,), daemon=True) for n in (1, 2)]
    t_monitor_start = time.time()
    for t in monitor_threads:
        t.start()

    time.sleep(BASELINE_S)

    log(f'=== starting {acq_duration:.0f}s T-mode timestamp acquisition on both nodes ===\n')
    acq_results = {}

    def _acquire(node_id):
        acq_results[node_id] = run_acquisition(node_id, acq_duration, log)

    acq_threads = [threading.Thread(target=_acquire, args=(n,), daemon=True) for n in (1, 2)]
    for t in acq_threads:
        t.start()
    for t in acq_threads:
        t.join()
    log('=== acquisition finished on both nodes; resource monitor still running ===\n')

    for t in monitor_threads:
        t.join()
    log('=== resource monitor finished on both nodes ===\n')

    for node_id in (1, 2):
        path = os.path.join(outdir, f'node{node_id}_resource_monitor.json')
        with open(path, 'w') as f:
            json.dump(monitor_results[node_id], f, indent=2)
        mnr.print_summary(monitor_results[node_id], log=log)

    combined = {
        'mask': mask_name,
        'monitor_duration_s': monitor_duration,
        'acq_duration_s': acq_duration,
        'baseline_s': BASELINE_S,
        't_monitor_start': t_monitor_start,
        'acquisitions': {str(n): acq_results[n] for n in (1, 2)},
    }
    combined_path = os.path.join(outdir, 'tmode_resource_experiment_summary.json')
    with open(combined_path, 'w') as f:
        json.dump(combined, f, indent=2)
    log(f'wrote {combined_path}\n')
    return monitor_results, acq_results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--mask', default='mask_sweep_40')
    ap.add_argument('--monitor-duration', type=float, default=120.0)
    ap.add_argument('--acq-duration', type=float, default=30.0)
    ap.add_argument('--outdir', default='figs')
    args = ap.parse_args()
    run_experiment(args.mask, args.monitor_duration, args.acq_duration, args.outdir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
