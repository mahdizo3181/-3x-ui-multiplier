"""Edge cases and stress, every behaviour on SQLite and on PostgreSQL: counters rewritten outside the panel's traffic
path (down and up), resets, long idle runs, mixed topologies, multiplier changes under load, heavy concurrent panel
writes, a daemon killed at random moments, fractions over thousands of requests, terabyte and int64-edge values.

    python3 -m unittest discover -s tests -p 'test_edge_cases.py'     (PostgreSQL: see test_xui_mult.py)"""
import json
import os
import random
import select
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import xui_mult as xm  # noqa: E402
from test_xui_mult import Base, PgBase  # noqa: E402

MB, GB, TB = 1 << 20, 1 << 30, 1 << 40


def x(v, k):
    """The extra that v bytes at multiplier k must cost, to the byte."""
    return v * (xm.mult_fp(k) - xm.SCALE) // xm.SCALE


class EdgeCases:
    def counters(self):
        return {e: (int(u), int(d)) for e, u, d in self.q("SELECT email, up, down FROM client_traffics")}

    def ledger_rows(self):
        return {r[0]: tuple(int(v) for v in r[1:]) for r in self.q(
            "SELECT email, last_up, last_down, rem_up, rem_down, raw_total, extra_total FROM xui_mult_ledger")}

    def snapshot(self):
        return sorted(self.q("SELECT email, up, down, enable FROM client_traffics")), \
            sorted(self.q("SELECT * FROM xui_mult_ledger"))

    # ------------------------------------------------------------------ counters rewritten outside the traffic path

    def test_usage_lowered_from_outside_is_never_billed(self):
        """The API, a bot or a restore lowers usage: 30 GB to 10 GB, by 100 MB, by one byte, upload only, down to one
        byte. Nothing is billed for it on any later tick, and billing carries on exactly from the new value."""
        drops = {"30-to-10gb": lambda u, d: (u // 2, 10 * GB), "minus-100mb": lambda u, d: (u, d - 100 * MB),
                 "minus-1-byte": lambda u, d: (u, d - 1), "upload-only": lambda u, d: (u - GB, d),
                 "to-1-byte": lambda u, d: (0, 1)}
        for e in drops:
            self.seed(e, [2], up=2 * GB, down=30 * GB)
        self.set(2, 1.2)
        self.tick()
        self.penv.add_traffic_batch([(e, 500, 1000) for e in drops])
        self.tick()
        extra, now = self.ledger_extra(), self.counters()
        lowered = {e: f(*now[e]) for e, f in drops.items()}
        for e, v in lowered.items():
            self.panel.set_traffic(e, *v)
        with self.assertLogs("xui-mult", "INFO") as logs:
            self.tick()
        for e in drops:
            self.assertIn(e, "\n".join(logs.output))
        for _ in range(10):
            self.tick()
        got = self.counters()
        for e, v in lowered.items():
            self.assertEqual(got[e], v, e)
        self.assertEqual(self.ledger_extra(), extra, "nothing billed for a drop")
        self.penv.add_traffic_batch([(e, 500, 1000) for e in drops])
        self.tick()
        got = self.counters()
        for e, (u, d) in lowered.items():
            self.assertEqual(got[e], (u + 600, d + 1200), e)

    def test_reset_of_every_client_starts_from_zero(self):
        """The panel's "reset all clients' traffic": every counter back to 0, then no spike on any later tick."""
        emails = [f"r{i}" for i in range(120)]
        self.penv.seed_many([(e, [2, 3], 5 * GB, 50 * GB) for e in emails])
        self.set(2, 1.2)
        self.set(3, 1.2)
        self.tick()
        self.penv.add_traffic_batch([(e, 1000, 5000) for e in emails])
        self.tick()
        extra = self.ledger_extra()
        self.panel.reset_all_traffic()
        for _ in range(20):
            self.tick()
        self.assertEqual(set(self.counters().values()), {(0, 0)})
        self.assertEqual(self.ledger_extra(), extra)
        self.penv.add_traffic_batch([(e, 1000, 5000) for e in emails])
        self.tick()
        self.tick()
        self.assertEqual(set(self.counters().values()), {(1200, 6000)})

    def test_traffic_between_a_reset_and_the_next_tick_is_billed_x1(self):
        """The price of never billing a drop: what is used between a reset and our next tick (seconds) costs x1."""
        self.seed("b", [2], down=10 * GB)
        self.set(2, 1.2)
        self.tick()
        self.panel.reset_traffic("b")
        self.panel.add_traffic("b", down=1000)
        self.tick()
        self.assertEqual(self.used("b"), (0, 1000))
        self.panel.add_traffic("b", down=1000)
        self.tick()
        self.assertEqual(self.used("b"), (0, 2200))

    def test_usage_raised_from_outside_is_not_billed(self):
        """The admin raises a client's usage by 50 GB (updateTraffic): far faster than any link, so a rewrite, not
        traffic. Followed, not billed, and named in the log."""
        self.seed("b", [2], down=GB)
        self.set(2, 1.2)
        self.tick()
        self.panel.set_traffic("b", 0, 51 * GB)
        with self.assertLogs("xui-mult", "WARNING") as logs:
            self.tick()
        self.assertIn("b (+50.00 GB in ", "\n".join(logs.output))
        for _ in range(10):
            self.tick()
        self.assertEqual(self.used("b"), (0, 51 * GB))
        self.assertEqual(self.ledger_extra(), 0)
        self.panel.add_traffic("b", down=1000)
        self.tick()
        self.assertEqual(self.used("b"), (0, 51 * GB + 1200))

    def test_imported_clients_are_never_billed_for_their_usage(self):
        """Clients imported with usage: a row that already holds it (first sight: history), and the way v3.9.0's
        import does it, the row created at 0 and its counters restored after, with a tick landing in between."""
        self.set(2, 1.2)
        self.seed("with-usage", [2], up=3 * GB, down=40 * GB)
        self.seed("restored-later", [2])
        self.tick()
        self.panel.set_traffic("restored-later", 3 * GB, 40 * GB)
        with self.assertLogs("xui-mult", "WARNING") as logs:
            self.tick()
        self.assertIn("restored-later (+43.00 GB in ", "\n".join(logs.output))
        for _ in range(5):
            self.tick()
        for e in ("with-usage", "restored-later"):
            self.assertEqual(self.used(e), (3 * GB, 40 * GB), e)
        self.assertEqual(self.ledger_extra(), 0)
        self.penv.add_traffic_batch([(e, 0, 1000) for e in ("with-usage", "restored-later")])
        self.tick()
        for e in ("with-usage", "restored-later"):
            self.assertEqual(self.used(e), (3 * GB, 40 * GB + 1200), e)

    def test_catching_up_after_a_day_of_downtime_is_billed_past_a_terabyte(self):
        """The daemon was down a day while a client moved 1.5 TB: real traffic over a long window (139 Mbit/s), so it
        is billed, exactly, on top of 5 TB of history; and ticks after it carry the fraction on."""
        self.seed("b", [2], up=TB, down=5 * TB)
        self.set(2, 1.337)
        self.tick()
        self.q("UPDATE xui_mult_ledger SET updated_at = updated_at - 86400")       # its last tick: a day ago
        up, down = 200 * GB, 3 * TB // 2
        self.panel.add_traffic("b", up=up, down=down)
        self.tick()
        self.assertEqual(self.used("b"), (TB + up + x(up, 1.337), 5 * TB + down + x(down, 1.337)))
        for _ in range(10):
            self.panel.add_traffic("b", down=1001)
            self.tick()
            down += 1001
        self.assertEqual(self.used("b")[1], 5 * TB + down + x(down, 1.337))

    # ------------------------------------------------------------------ idle stability

    def test_150_idle_clients_do_not_drift_over_50_ticks(self):
        """After one billed tick (odd bytes: remainders sit in the ledger), 50 ticks with no traffic leave every
        counter and every ledger column exactly as they were."""
        emails = [f"i{i}" for i in range(150)]
        self.penv.seed_many([(e, [2] if i % 2 else [2, 3], i * GB, 3 * i * GB) for i, e in enumerate(emails)])
        self.set(2, 1.2)
        self.set(3, 1.5)
        self.tick()
        self.penv.add_traffic_batch([(e, 7, 13) for e in emails])
        self.tick()
        before = self.snapshot()
        for _ in range(50):
            self.tick()
        self.assertEqual(self.snapshot(), before)

    def test_active_and_idle_clients_on_one_inbound(self):
        """120 clients on one inbound, a third of them never active, the rest active on random ticks: each active one
        pays exactly k on its own traffic, the idle ones never move."""
        emails = [f"m{i}" for i in range(120)]
        self.penv.seed_many([(e, [2], GB, 2 * GB) for e in emails])
        self.set(2, 1.25)
        self.tick()
        rng, raw, idle = random.Random(7), {e: [0, 0] for e in emails}, set(emails[::3])
        for _ in range(30):
            batch = [(e, rng.randrange(0, 50_000), rng.randrange(0, 500_000))
                     for e in emails if e not in idle and rng.random() < 0.5]
            self.penv.add_traffic_batch(batch)
            for e, u, d in batch:
                raw[e][0] += u
                raw[e][1] += d
            self.tick()
        got = self.counters()
        for e, (u, d) in raw.items():
            self.assertEqual(got[e], (GB + u + x(u, 1.25), 2 * GB + d + x(d, 1.25)), e)

    # ------------------------------------------------------------------ topologies

    def test_mixed_multipliers_pay_the_highest_with_exact_fractions(self):
        """#2 at 1.2, #3 at 1.537, #1 not multiplied: on all three pays 1.537, on #2 + #1 pays 1.2, on #1 alone x1.
        Over 400 ticks of odd byte counts the extra is floor(raw x (k - 1)), and the ledger says so too."""
        ks = {2: 1.2, 3: 1.537}
        topo = {"abc": [2, 3, 1], "ab": [3, 2], "ac": [1, 2], "c": [1]}
        pays = {"abc": 1.537, "ab": 1.537, "ac": 1.2, "c": 1.0}
        for e, ibs in topo.items():
            self.seed(e, ibs)
        db = xm.db_connect(self.db)
        xm.bill(db, ks)
        rng, raw = random.Random(3), dict.fromkeys(topo, 0)
        for _ in range(400):
            batch = [(e, 0, rng.randrange(1, 1500)) for e in topo]
            self.penv.add_traffic_batch(batch)
            for e, _, d in batch:
                raw[e] += d
            xm.bill(db, ks)
        led = self.ledger_rows()
        for e, k in pays.items():
            self.assertEqual(self.used(e), (0, raw[e] + x(raw[e], k)), e)
            if k > 1:
                self.assertEqual(led[e][4:], (raw[e], x(raw[e], k)), e)
        self.assertNotIn("c", led)

    def test_multipliers_changed_while_traffic_flows(self):
        """The real daemon loop, the panel writing traffic non-stop, and the multipliers changed under it: #2 1.2 ->
        1.5, #3 1.3 added, #2 removed, #3 removed, #2 1.2 again. Every credited byte is in the ledger, no traffic is
        lost or counted twice, nothing is billed while unmultiplied, and billing restarts from the counters."""
        emails = [f"f{i}" for i in range(20)]
        self.penv.seed_many([(e, [2, 3], 0, 0) for e in emails])
        raw, lock, stop, errors = dict.fromkeys(emails, 0), threading.Lock(), threading.Event(), []

        def writer():
            rng = random.Random(11)
            try:
                while not stop.is_set():
                    batch = [(e, 0, rng.randrange(1, 50_000)) for e in rng.sample(emails, 8)]
                    with lock:
                        self.penv.add_traffic_batch(batch)
                        for e, _, d in batch:
                            raw[e] += d
                    time.sleep(0.002)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def settle(wait):
            """Pause the writer for two ticks: {email: (credited so far, traffic so far, ledger raw, ledger extra)}."""
            with lock:
                wait(2)
                got, led = self.counters(), self.ledger_rows()
                return {e: (got[e][1] - raw[e], raw[e]) + (led[e][4:] if e in led else (None, None)) for e in emails}

        def script(wait):
            self.set(2, 1.2)
            wait(2)                                   # first sight at 0: from here every byte is billed
            th = threading.Thread(target=writer)
            th.start()
            try:
                wait(4)
                self.set(2, 1.5)
                wait(4)
                self.set(3, 1.3)
                wait(3)
                self.quiet(xm.op_remove, 2)           # #3 at 1.3 still owns them: the ledger stays
                wait(4)
                for e, (credited, sent, lraw, lextra) in settle(wait).items():
                    self.assertEqual(credited, lextra, e)
                    self.assertEqual(sent, lraw, e)
                    self.assertTrue(x(sent, 1.2) <= credited <= x(sent, 1.5), (e, sent, credited))
                self.quiet(xm.op_remove, 3)           # nothing multiplied: ledger pruned
                wait(3)
                frozen = {e: v[0] for e, v in settle(wait).items()}
                wait(6)
                after = settle(wait)
                self.assertEqual({e: v[0] for e, v in after.items()}, frozen, "billed while unmultiplied")
                self.assertTrue(all(v[2] is None for v in after.values()))
                self.set(2, 1.2)
                wait(6)
                for e, (credited, sent, lraw, lextra) in settle(wait).items():
                    self.assertEqual(credited - frozen[e], lextra, e)
                    self.assertEqual(lextra, x(lraw, 1.2), e)
            finally:
                stop.set()
                th.join()

        self.run_daemon_with(script, timeout=120)
        self.assertEqual(errors, [])

    # ------------------------------------------------------------------ concurrency

    def test_heavy_concurrent_panel_writes_are_billed_exactly(self):
        """8 panel threads hammer 60 clients with atomic adds (on PostgreSQL each holds its row lock a moment) while
        the real daemon loop bills: every byte is billed exactly once at k. SQLite runs in WAL with a busy timeout;
        on PostgreSQL the ticks must have met locked rows and skipped them (SKIP LOCKED), billing them later."""
        emails = [f"h{i}" for i in range(60)]
        self.penv.seed_many([(e, [2, 3], 0, 0) for e in emails])
        self.set(2, 1.2)
        self.set(3, 1.2)
        if self.kind == "sqlite":
            self.assertEqual(self.q("PRAGMA journal_mode")[0][0], "wal")
        raw, lock, stop, errors = {e: [0, 0] for e in emails}, threading.Lock(), threading.Event(), []
        hold = {"pause": 0.002} if self.kind == "postgres" else {}

        def writer(seed):
            rng = random.Random(seed)
            try:
                while not stop.is_set():
                    e, u, d = rng.choice(emails), rng.randrange(0, 3000), rng.randrange(1, 30_000)
                    self.panel.add_traffic(e, up=u, down=d, **hold)
                    with lock:
                        raw[e][0] += u
                        raw[e][1] += d
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def script(wait):
            wait(2)
            ths = [threading.Thread(target=writer, args=(s,)) for s in range(8)]
            [t.start() for t in ths]
            time.sleep(4)
            stop.set()
            [t.join() for t in ths]
            wait(3)

        real, met_locked = xm.Database.counters, [0]

        def spy(db, ids, lock=False):
            got = real(db, ids, lock)
            met_locked[0] += lock and len(got) < len(emails)
            return got

        with mock.patch.object(xm.Database, "counters", spy):
            self.run_daemon_with(script)
        self.assertEqual(errors, [])
        got = self.counters()
        for e, (u, d) in raw.items():
            self.assertEqual(got[e], (u + x(u, 1.2), d + x(d, 1.2)), e)
        self.assertGreater(sum(d for _, d in raw.values()), 0)
        if self.kind == "postgres":
            self.assertGreater(met_locked[0], 0, "no tick met a locked row: SKIP LOCKED was never exercised")

    def test_daemon_killed_at_random_moments_never_loses_or_doubles_a_byte(self):
        """A real daemon process SIGKILLed 10 times while the panel writes traffic, restarted each time: 5 times the
        moment it reports an open transaction (credit and ledger written, not committed), 5 at random moments. Then
        one more is stopped with SIGTERM mid-run (it finishes its tick and exits 0). Every byte billed exactly once,
        ledger consistent."""
        emails = [f"k{i}" for i in range(15)]
        self.penv.seed_many([(e, [2], 0, 0) for e in emails])
        self.set(2, 1.2)
        self.tick()                                   # first sight at 0
        child = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {os.path.dirname(HERE)!r})
            import xui_mult as xm
            xm.CONF_DIR, xm.RUN_DIR, xm.CONF_PATH, xm.STATUS_PATH = {xm.CONF_DIR!r}, {xm.RUN_DIR!r}, \\
                {xm.CONF_PATH!r}, {xm.STATUS_PATH!r}
            xm.PANEL_ENV_FILES = ()
            load, apply = xm.load_config, xm.Database.apply
            def fast():
                cfg = load()
                cfg["interval"] = 0.02
                return cfg
            def slow(db, plan):
                apply(db, plan)
                print("in-tx", flush=True)            # written, not committed: a kill now must leave no trace
                time.sleep(0.03)
            xm.load_config, xm.Database.apply = fast, slow
            class A:
                once = dry_run = False
            xm.run_daemon(A())
        """)
        raw, stop, errors = {e: 0 for e in emails}, threading.Event(), []

        def writer():
            rng = random.Random(13)
            try:
                while not stop.is_set():
                    e, d = rng.choice(emails), rng.randrange(1, 20_000)
                    self.panel.add_traffic(e, down=d)
                    raw[e] += d
                    time.sleep(0.003)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def start():
            return subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True)

        th = threading.Thread(target=writer)
        th.start()
        rng = random.Random(17)
        try:
            for i in range(10):
                p = start()
                if i % 2:
                    time.sleep(rng.uniform(0.4, 0.9))
                else:
                    self.assertTrue(select.select([p.stdout], [], [], 30)[0], "the daemon never opened a transaction")
                    p.stdout.readline()
                p.kill()
                p.wait()
                p.stdout.close()
                p.stderr.close()
            p = start()
            time.sleep(0.8)
            p.send_signal(signal.SIGTERM)
            _, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err)
        finally:
            stop.set()
            th.join()
        self.assertEqual(errors, [])
        self.tick()
        self.tick()
        got, led = self.counters(), self.ledger_rows()
        for e, d in raw.items():
            self.assertEqual(got[e], (0, d + x(d, 1.2)), e)
            self.assertEqual(led[e][4:], (d, x(d, 1.2)), e)
        self.assertGreater(sum(raw.values()), 0)

    # ------------------------------------------------------------------ data integrity, boundary values

    def test_thousands_of_tiny_requests_lose_no_byte(self):
        """3000 HTTP-sized reports (1 B to 1.5 KB) at 1.001x, 1.234x and 9.999x, billed every few reports: the extra
        is floor(raw x (k - 1)) to the byte, and what is carried over is exactly the remainder, below one byte."""
        self.add_inbounds(4)
        ks, names = {2: 1.001, 3: 1.234, 4: 9.999}, {2: "t2", 3: "t3", 4: "t4"}
        for ib, e in names.items():
            self.seed(e, [ib])
        db = xm.db_connect(self.db)
        xm.bill(db, ks)
        rng, raw, pending = random.Random(5), {e: [0, 0] for e in names.values()}, []
        for _ in range(3000):
            e, u, d = rng.choice(list(raw)), rng.randrange(1, 300), rng.randrange(1, 1500)
            pending.append((e, u, d))
            raw[e][0] += u
            raw[e][1] += d
            if rng.random() < 0.2:
                self.penv.add_traffic_batch(pending)
                pending = []
                xm.bill(db, ks)
        self.penv.add_traffic_batch(pending)
        xm.bill(db, ks)
        led = self.ledger_rows()
        for ib, e in names.items():
            u, d = raw[e]
            k = xm.mult_fp(ks[ib]) - xm.SCALE
            self.assertEqual(self.used(e), (u + x(u, ks[ib]), d + x(d, ks[ib])), e)
            self.assertEqual(led[e][2:4], (u * k % xm.SCALE, d * k % xm.SCALE), e)

    def test_credit_that_would_pass_the_cap_stops_exactly_at_it(self):
        """A counter 120 MB below the panel's cap (TrafficMax, below int64) at 3x: 50 MB of traffic would credit 100 MB;
        it gets exactly the 70 MB that fill it to the cap, and the ledger records 70 MB. Nothing errors on either
        database (PostgreSQL raises on bigint overflow)."""
        self.seed("b", [2], down=xm.TRAFFIC_MAX - 120 * MB)
        self.set(2, 3)
        self.tick()
        self.panel.add_traffic("b", down=50 * MB)
        self.tick()
        self.assertEqual(self.status()["error"], "")
        self.assertEqual(self.used("b"), (0, xm.TRAFFIC_MAX))
        self.assertEqual(self.ledger_extra(), 70 * MB)
        self.panel.add_traffic("b", down=MB)
        self.tick()
        self.assertEqual(self.status()["error"], "")
        self.assertLessEqual(self.used("b")[1], 2 ** 63 - 1)
        self.assertEqual(self.ledger_extra(), 70 * MB)


class TestEdgeCasesSQLite(EdgeCases, Base):
    pass


class TestEdgeCasesPostgres(EdgeCases, PgBase):
    pass


class TestRateLimit(Base):
    """How fast a counter may move before it counts as rewritten: exact at the boundary, per direction, over the
    right window. compute_plan is pure, so the clock is ours."""
    M = {"b": (1200, 2, (2,))}
    LIM = 125_000_000                             # 1000 Mbit/s in bytes/s

    def plan(self, up, down, updated_at=1000, now=1007, synced_at=None, max_bps=LIM):
        return xm.compute_plan(self.M, {"b": (up, down)}, {"b": (0, 0, 0, 0, 0, 0, updated_at)}, now, synced_at,
                               max_bps)

    def test_boundary(self):
        lim = 7 * self.LIM
        self.assertEqual(self.plan(0, lim).credits, [("b", 0, lim // 5)])
        p = self.plan(0, lim + 1)
        self.assertEqual((p.credits, len(p.jumped)), ([], 1))
        self.assertEqual(p.moves[0][1:3], (0, lim + 1), "followed, not billed")

    def test_each_direction_has_its_own_limit(self):
        lim = 7 * self.LIM                        # full duplex: both at line rate at once is real
        self.assertEqual(self.plan(lim, lim).credits, [("b", lim // 5, lim // 5)])
        self.assertTrue(self.plan(lim + 1, 0).jumped)

    def test_window_starts_at_the_later_of_the_ledger_move_and_the_last_clean_tick(self):
        # an idle client (ledger untouched for a day) whose counter jumps 10 GB since the tick 7 s ago: a rewrite
        self.assertTrue(self.plan(0, 10 * GB, updated_at=0, now=86_400, synced_at=86_393).jumped)
        # no clean tick yet on this connection (the daemon just started): the whole day is the window
        self.assertFalse(self.plan(0, 10 * GB, updated_at=0, now=86_400).jumped)
        # another biller moved the ledger after our last clean tick: the window starts there
        self.assertTrue(self.plan(0, 2 * self.LIM, updated_at=86_399, now=86_400, synced_at=86_000).jumped)

    def test_a_clock_going_backwards_still_has_a_one_second_window(self):
        self.assertTrue(self.plan(0, self.LIM + 1, updated_at=2000, now=1000).jumped)
        self.assertFalse(self.plan(0, self.LIM, updated_at=2000, now=1000).jumped)

    def test_zero_means_no_limit(self):
        self.assertEqual(self.plan(0, TB, max_bps=0).credits, [("b", 0, TB // 5)])

    def test_the_daemon_uses_the_configured_limit_and_the_config_is_validated(self):
        def configure(v):
            with open(xm.CONF_PATH) as f:
                cfg = json.load(f)
            cfg["max_mbps"] = v
            with open(xm.CONF_PATH, "w") as f:
                json.dump(cfg, f)
        configure(-1)
        with self.assertRaises(xm.XMError):
            xm.load_config()
        configure(0.001)                          # 125 bytes/s
        self.seed("b", [2])
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", down=10_000)
        with self.assertLogs("xui-mult", "WARNING"):
            self.tick()
        self.assertEqual(self.used("b"), (0, 10_000))


if __name__ == "__main__":
    unittest.main()
