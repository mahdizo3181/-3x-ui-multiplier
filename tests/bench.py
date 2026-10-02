"""Benchmark: tick time, statements and memory against client count, on SQLite and PostgreSQL.

    python3 tests/bench.py                      sizes 100,500,1000,2500 per multiplied inbound, both databases
    python3 tests/bench.py --sizes 500,5000 --backends postgres
    python3 tests/bench.py --rtt-ms 1           add 1 ms of network latency per statement (a database on another host)

Also runs two baselines on the same data, so the gain is measured rather than claimed:
  v2.1   the previous release's tick (SQLite only; loaded from git history if available)
  naive  the same plan written to PostgreSQL one statement per client (what a straight port would do)
"""
import argparse
import gc
import importlib.util
import os
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import mock_panel as mp  # noqa: E402
import pgcluster  # noqa: E402
import scale_data as sd  # noqa: E402
import xui_mult as xm  # noqa: E402


def load_v21():
    """The previous release's module, from git history (None if there is no git history or it is gone)."""
    try:
        src = subprocess.run(["git", "show", "v2.1.0:xui_mult.py"], cwd=os.path.dirname(HERE), capture_output=True,
                             text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            src = subprocess.run(["git", "show", "8498beb:xui_mult.py"], cwd=os.path.dirname(HERE),
                                 capture_output=True, text=True, check=True).stdout
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
    path = os.path.join(tempfile.mkdtemp(), "xui_mult_v21.py")
    open(path, "w").write(src)
    spec = importlib.util.spec_from_file_location("xui_mult_v21", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class NaivePg(xm.PostgresDB):
    """Same plan, one statement per client."""
    def credit(self, rows):
        for e, u, d in rows:
            self.run("UPDATE client_traffics SET up = LEAST(up + ?, ?), down = LEAST(down + ?, ?) WHERE email = ?",
                     (u, xm.TRAFFIC_MAX, d, xm.TRAFFIC_MAX, e))

    def ledger_update(self, rows):
        for e, lu, ld, ru, rd, rt, et, ts in rows:
            self.run("UPDATE xui_mult_ledger SET last_up=?, last_down=?, rem_up=?, rem_down=?, raw_total=?, "
                     "extra_total=?, updated_at=? WHERE email=?", (lu, ld, ru, rd, rt, et, ts, e))


def with_latency(db, rtt):
    """Make every statement cost one network round trip of `rtt` seconds (PostgreSQL only)."""
    if rtt:
        real_rows = db.rows

        def rows(sql, args=()):
            time.sleep(rtt)
            return real_rows(sql, args)
        db.rows = rows
    return db


def ms(x):
    return f"{x * 1000:.1f}"


def timed(fn):
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def run(kind, per, tmp, rtt=0.0):
    env = mp.make_env(kind, tmp)
    members = sd.build(env, per, per // 6, 2000)
    multiplied = len({e for e, ids in members.items() if set(ids) & set(sd.K)})
    db = with_latency(xm.db_connect(env.ref), rtt if kind == "postgres" else 0)
    xm.bill(db, sd.K)                                          # baseline
    env.add_traffic_batch(sd.traffic(members, 1.0, seed=3))
    busy = timed(lambda: xm.bill(db, sd.K))                    # time and memory on separate ticks:
    stmts = db.last["statements"]                              # tracemalloc slows allocation-heavy code several-fold
    env.add_traffic_batch(sd.traffic(members, 1.0, seed=8))
    gc.collect()
    tracemalloc.start()
    xm.bill(db, sd.K)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    idle = statistics.median(timed(lambda: xm.bill(db, sd.K)) for _ in range(20))
    few = []
    for i in range(5):
        env.add_traffic_batch(sd.traffic(members, 0.03, seed=20 + i))
        few.append(timed(lambda: xm.bill(db, sd.K)))
    row = {"kind": kind + (f" +{rtt * 1000:g} ms RTT" if rtt else ""), "clients": multiplied, "idle": idle, "few": statistics.median(few), "busy": busy,
           "stmts": stmts, "peak": peak / 2 ** 20}

    # baselines, on a copy of the same situation
    env.add_traffic_batch(sd.traffic(members, 1.0, seed=4))
    if kind == "sqlite" and V21:
        old = V21.db_connect(env.ref)
        V21.bill(old, sd.K)
        env.add_traffic_batch(sd.traffic(members, 1.0, seed=5))
        row["v21_busy"] = timed(lambda: V21.bill(old, sd.K))
        row["v21_idle"] = statistics.median(timed(lambda: V21.bill(old, sd.K)) for _ in range(20))
        old.close()
    if kind == "postgres":
        naive = with_latency(NaivePg.connect(env.ref), rtt)
        xm.bill(naive, sd.K)
        env.add_traffic_batch(sd.traffic(members, 1.0, seed=6))
        row["naive_busy"] = timed(lambda: xm.bill(naive, sd.K))
        row["naive_stmts"] = naive.last["statements"]
        naive.close()
    db.close()
    env.close()
    return row


def main():
    global V21
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="100,500,1000,2500", help="clients per multiplied inbound (comma list)")
    ap.add_argument("--backends", default="sqlite,postgres")
    ap.add_argument("--rtt-ms", type=float, default=0.0, help="simulated network latency per PostgreSQL statement")
    args = ap.parse_args()
    kinds = args.backends.split(",")
    if "postgres" in kinds:
        ok, why = pgcluster.available()
        if not ok:
            print("skipping PostgreSQL:", why)
            kinds.remove("postgres")
    V21 = load_v21()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for kind in kinds:
            for per in map(int, args.sizes.split(",")):
                sub = tempfile.mkdtemp(dir=tmp)
                rows.append(run(kind, per, sub, args.rtt_ms / 1000))
    print("\n| database | multiplied clients | idle tick | 3% active | all active | statements | peak memory | baseline (all active) |")
    print("|---|---:|---:|---:|---:|---:|---:|---|")
    for r in rows:
        base = ""
        if "v21_busy" in r:
            base = f"v2.1: {ms(r['v21_busy'])} ms (idle {ms(r['v21_idle'])} ms)"
        if "naive_busy" in r:
            base = f"per-client: {ms(r['naive_busy'])} ms, {r['naive_stmts']} statements"
        print(f"| {r['kind']} | {r['clients']} | {ms(r['idle'])} ms | {ms(r['few'])} ms | {ms(r['busy'])} ms | "
              f"{r['stmts']} | {r['peak']:.1f} MiB | {base} |")


V21 = None
if __name__ == "__main__":
    main()
