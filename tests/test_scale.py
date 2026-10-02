"""Scale: 500+ clients per multiplied inbound, on SQLite and on PostgreSQL.

Proves what matters for a busy panel: every client is billed to the byte, a tick is a fixed handful of
statements however many clients there are, an idle tick takes no lock and writes nothing, and time and memory
stay small. The time limits are deliberately loose (they must hold on a loaded CI box); `python3 tests/bench.py`
prints the real numbers."""
import gc
import statistics
import sys
import time
import tracemalloc
import unittest

import scale_data as sd
import test_xui_mult as base
import xui_mult as xm

BUSY_LIMIT_S, IDLE_LIMIT_S, PEAK_LIMIT_MIB = 2.0, 0.5, 40


class ScaleTests:
    PER, OVERLAP, NOISE = 600, 100, 2000          # 1100 distinct multiplied clients, 3000 in the panel

    def setUp(self):
        super().setUp()
        self.members = sd.build(self.penv, self.PER, self.OVERLAP, self.NOISE)
        self.billed = {e for e, ids in self.members.items() if set(ids) & set(sd.K)}
        self.cfg = dict(sd.K)
        self.db_ = xm.db_connect(self.db)
        xm.bill(self.db_, self.cfg)                                    # baseline: history is never billed
        self.assertEqual(self.db_.last["clients"], len(self.billed))

    def tearDown(self):
        self.db_.close()
        super().tearDown()

    def counters(self):
        return {e: (u, d) for e, u, d in self.q("SELECT email, up, down FROM client_traffics")}

    def report(self, what, **kw):
        print(f"\n  scale[{self.kind}] {what}: " + ", ".join(f"{k}={v}" for k, v in kw.items()),
              file=sys.stderr, end="")

    def test_every_client_is_billed_exactly(self):
        deltas = sd.traffic(self.members, share=1.0)
        self.penv.add_traffic_batch(deltas)
        after_panel = self.counters()
        t0 = time.perf_counter()
        per_ib, skipped = xm.bill(self.db_, self.cfg)
        busy = time.perf_counter() - t0
        extra = sd.expected_extra(self.members, deltas)
        after = self.counters()
        wrong = [e for e in self.members
                 if after[e] != (after_panel[e][0] + extra.get(e, (0, 0))[0], after_panel[e][1] + extra.get(e, (0, 0))[1])]
        self.assertEqual((skipped, wrong), ([], []))
        # the overlapping clients pay the higher multiplier, so they are billed (and counted) on inbound #3
        self.assertEqual((per_ib[2][0], per_ib[3][0]), (self.PER - self.OVERLAP, self.PER))
        self.report("busy tick", clients=len(self.billed), ms=round(busy * 1000), statements=self.db_.last["statements"])
        self.assertLess(busy, BUSY_LIMIT_S)
        self.assertLessEqual(self.db_.last["statements"], 14, "a tick must stay a fixed handful of statements")

    def test_idle_tick_is_cheap_and_writes_nothing(self):
        xm.bill(self.db_, self.cfg)
        marker, times = self.write_marker(self.db_), []
        for _ in range(15):
            t0 = time.perf_counter()
            self.assertEqual(xm.bill(self.db_, self.cfg), ({}, []))
            times.append(time.perf_counter() - t0)
        self.assertEqual(self.write_marker(self.db_), marker, "an idle tick wrote something")
        self.assertEqual(self.db_.last["statements"], 3, "membership, counters, ledger: nothing else, no transaction")
        self.report("idle tick", clients=len(self.billed), median_ms=round(statistics.median(times) * 1000, 1))
        self.assertLess(statistics.median(times), IDLE_LIMIT_S)

    def test_a_quiet_panel_with_a_few_active_clients(self):
        """The common case: 1100 multiplied clients, a few dozen with traffic this tick."""
        deltas = sd.traffic(self.members, share=0.03, seed=7)
        self.penv.add_traffic_batch(deltas)
        t0 = time.perf_counter()
        xm.bill(self.db_, self.cfg)
        self.report("few active", active=len(deltas), ms=round((time.perf_counter() - t0) * 1000),
                    statements=self.db_.last["statements"])
        self.assertLessEqual(self.db_.last["statements"], 14)

    def test_sustained_ticks_stay_exact(self):
        """Fifteen ticks, a third of the clients active each time."""
        panel_up, expect_extra = self.counters(), {}
        for n in range(15):
            deltas = sd.traffic(self.members, share=0.33, seed=100 + n)
            self.penv.add_traffic_batch(deltas)
            for e, (xu, xd) in sd.expected_extra(self.members, deltas).items():
                a, b = expect_extra.get(e, (0, 0))
                expect_extra[e] = (a + xu, b + xd)
            xm.bill(self.db_, self.cfg)
        final = self.counters()
        raw = {e: (final[e][0] - expect_extra.get(e, (0, 0))[0], final[e][1] - expect_extra.get(e, (0, 0))[1])
               for e in self.members}
        led = {e: (rt, et) for e, rt, et in self.q("SELECT email, raw_total, extra_total FROM xui_mult_ledger")}
        self.assertEqual(len(led), len(self.billed))
        self.assertEqual(sum(et for _, et in led.values()), sum(a + b for a, b in expect_extra.values()))
        for e in list(self.billed)[:50]:
            self.assertEqual(led[e][0], (raw[e][0] - panel_up[e][0]) + (raw[e][1] - panel_up[e][1]), e)

    def test_memory_footprint(self):
        self.penv.add_traffic_batch(sd.traffic(self.members, share=1.0, seed=9))
        gc.collect()
        tracemalloc.start()
        xm.bill(self.db_, self.cfg)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.report("memory", clients=len(self.billed), peak_MiB=round(peak / 2 ** 20, 1))
        self.assertLess(peak / 2 ** 20, PEAK_LIMIT_MIB)


class TestScaleSQLite(ScaleTests, base.Base):
    pass


class TestScalePostgres(ScaleTests, base.PgBase):
    def test_lookups_use_the_panels_indexes(self):
        self.q("ANALYZE")
        cond, args = self.db_.any_of("ci.inbound_id", [2, 3])
        plan = "\n".join(r[0] for r in self.db_.rows(
            "EXPLAIN SELECT t.email, t.up, t.down FROM client_traffics t WHERE t.email IN (SELECT c.email FROM clients c "
            f"JOIN client_inbounds ci ON ci.client_id = c.id WHERE {cond})", args))
        self.assertIn("Index", plan, plan)
        self.assertNotIn("Seq Scan on client_inbounds", plan, plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)
