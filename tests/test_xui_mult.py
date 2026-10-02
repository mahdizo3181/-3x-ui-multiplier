"""Unit tests against a mock of the 3X-UI v3.8.5 database, on SQLite and on a real PostgreSQL server.

Every behavioural test is written once (a mixin) and runs on both backends; PostgreSQL is a throwaway local
cluster (tests/pgcluster.py) and its tests skip, saying why, when the server binaries or the driver are missing."""
import contextlib
import io
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import xui_mult as xm  # noqa: E402
import mock_panel as mp  # noqa: E402
import pgcluster  # noqa: E402

PG_OK, PG_WHY = pgcluster.available()


class Base(unittest.TestCase):
    """Inbounds: #1 Direct, #2 Germany Tunnel, #3 Tunnel 2 (see mock_panel)."""
    kind = "sqlite"

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"COLUMNS": "120"})   # deterministic wrapping
        self.env.start()
        for k in ("XUI_DB_TYPE", "XUI_DB_DSN", "XUI_DB_FOLDER", "XUI_MULT_DB"):
            os.environ.pop(k, None)
        self.no_host_env = mock.patch.object(xm, "PANEL_ENV_FILES", ())    # never read the test host's /etc/default/x-ui
        self.no_host_env.start()
        self.tmp = tempfile.mkdtemp()
        xm.CONF_DIR = os.path.join(self.tmp, "etc")
        xm.RUN_DIR = os.path.join(self.tmp, "run")
        xm.CONF_PATH = os.path.join(xm.CONF_DIR, "config.json")
        xm.STATUS_PATH = os.path.join(xm.RUN_DIR, "status.json")
        xm.C.on = False
        self._busy = xm.BUSY_TIMEOUT_MS
        self.penv = mp.make_env(self.kind, self.tmp)
        self.db, self.panel = self.penv.ref, self.penv.panel
        self.q, self.used, self.seed = self.penv.q, self.penv.used, self.penv.seed
        os.makedirs(xm.CONF_DIR)
        with open(xm.CONF_PATH, "w") as f:
            json.dump(dict(xm.DEFAULT_CONFIG, db=self.db), f)

    def tearDown(self):
        self.env.stop()
        self.no_host_env.stop()
        xm.C.on = False
        xm.BUSY_TIMEOUT_MS = self._busy
        self.penv.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tick(self, dry_run=False):
        class A:
            once = True
        A.dry_run = dry_run
        xm._stop = False
        xm.run_daemon(A())

    def quiet(self, fn, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            r = fn(*a, **kw)
        return r, out.getvalue()

    def set(self, ib, k):
        self.quiet(xm.op_set, ib, k)

    def status(self):
        with open(xm.STATUS_PATH) as f:
            return json.load(f)

    def run_daemon_with(self, script, timeout=60):
        """Run the real daemon loop (50 ms interval) in this thread while `script(wait_ticks)` runs in another and
        may change things between ticks. The daemon stops when the script returns."""
        real, ticks, errors = xm.load_config, [0], []

        def fast():
            cfg = real()
            cfg["interval"] = 0.05
            return cfg

        def notify(msg):
            ticks[0] += "WATCHDOG" in msg

        def wait_ticks(n=2):
            goal, t0 = ticks[0] + n, time.time()
            while ticks[0] < goal and time.time() - t0 < timeout:
                time.sleep(0.01)
            self.assertGreaterEqual(ticks[0], goal, "the daemon stopped ticking")

        def helper():
            try:
                script(wait_ticks)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
            finally:
                xm._stop = True

        class A:
            once = False
            dry_run = False
        xm._stop = False
        th = threading.Thread(target=helper)
        with mock.patch.object(xm, "load_config", fast), mock.patch.object(xm, "sd_notify", notify):
            th.start()
            xm.run_daemon(A())
        th.join()
        if errors:
            raise errors[0]

    def add_inbounds(self, *ids):
        for i in ids:
            self.q("INSERT INTO inbounds(id, remark, protocol, port, tag, enable) VALUES (?,?,?,?,?,?)",
                   (i, f"Tunnel {i}", "vless", 9000 + i, f"in-{i}", True))

    def ledger_extra(self):
        return int(self.q("SELECT COALESCE(SUM(extra_total), 0) FROM xui_mult_ledger")[0][0])

    # backend hooks used by the shared tests
    def panel_settings(self):
        """The XUI_DB_* settings a panel running on this database would have."""
        return {"XUI_DB_FOLDER": self.tmp}

    def write_marker(self, db):
        """Changes whenever anything was written to the database."""
        return db.total_changes

    def break_ledger(self):
        self.q("CREATE TRIGGER boom BEFORE UPDATE ON xui_mult_ledger "
               "BEGIN SELECT RAISE(ABORT, 'simulated crash'); END")

    def fix_ledger(self):
        self.q("DROP TRIGGER boom")


class PgBase(Base):
    kind = "postgres"

    @classmethod
    def setUpClass(cls):
        if not PG_OK:
            raise unittest.SkipTest("PostgreSQL tests: " + PG_WHY)

    def panel_settings(self):
        return {"XUI_DB_TYPE": "postgres", "XUI_DB_DSN": self.db}

    def write_marker(self, db):
        return self.q("SELECT pg_current_wal_lsn()::text")[0][0]

    def break_ledger(self):
        self.q("CREATE FUNCTION xm_boom() RETURNS trigger LANGUAGE plpgsql AS "
               "$$ BEGIN RAISE EXCEPTION 'simulated crash'; END $$")
        self.q("CREATE TRIGGER boom BEFORE UPDATE ON xui_mult_ledger FOR EACH ROW EXECUTE FUNCTION xm_boom()")

    def fix_ledger(self):
        self.q("DROP TRIGGER boom ON xui_mult_ledger")


# =================================================================================== behaviour, on both databases

class BillingTests:
    def test_tunnel_client_pays_k(self):
        self.seed("b", [2])
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", up=1000, down=5000)
        self.tick()
        self.assertEqual(self.used("b"), (1200, 6000))
        self.tick()
        self.assertEqual(self.used("b"), (1200, 6000), "our own credit is never billed again")

    def test_other_inbounds_untouched(self):
        self.seed("a", [1])
        self.seed("c", [3])
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("a", down=1000)
        self.panel.add_traffic("c", down=1000)
        self.tick()
        self.assertEqual(self.used("a"), (0, 1000))
        self.assertEqual(self.used("c"), (0, 1000))

    def test_membership_comes_from_client_inbounds_not_the_stale_pointer(self):
        self.seed("m", [1, 2])      # client_traffics.inbound_id says 1
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("m", down=1000)
        self.tick()
        self.assertEqual(self.used("m"), (0, 1200))
        _, out = self.quiet(xm.op_list)
        self.assertIn("inbound #2: 1 client(s) are also on a lower-multiplier inbound", out)

    def test_highest_multiplier_wins(self):
        self.seed("d", [2, 3])
        self.set(2, 1.2)
        self.set(3, 1.5)
        self.tick()
        self.panel.add_traffic("d", down=1000)
        self.tick()
        self.assertEqual(self.used("d"), (0, 1500))

    def test_new_clients_are_picked_up_and_history_is_never_billed(self):
        self.seed("old", [2], down=10_000)        # used before the multiplier was set
        self.set(2, 1.2)
        self.tick()
        self.assertEqual(self.used("old"), (0, 10_000))
        self.seed("new", [2])                      # created later in the panel
        self.tick()
        for e in ("old", "new"):
            self.panel.add_traffic(e, down=1000)
        self.tick()
        self.assertEqual(self.used("old"), (0, 11_200))
        self.assertEqual(self.used("new"), (0, 1200))

    def test_fractions_carry_over(self):
        self.seed("b", [2])
        self.set(2, 1.234)
        db = xm.db_connect(self.db)
        xm.bill(db, {2: 1.234})
        for _ in range(300):
            self.panel.add_traffic("b", down=3)                  # 3 x 0.234 = 0.702 extra per tick
            xm.bill(db, {2: 1.234})
        self.assertEqual(self.used("b"), (0, 900 + 900 * 234 // 1000))

    def test_panel_reset_is_followed(self):
        self.seed("b", [2])
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", down=1000)
        self.tick()
        self.panel.reset_traffic("b")                            # renewal
        self.tick()
        self.assertEqual(self.used("b"), (0, 0))
        self.panel.add_traffic("b", down=500)
        self.tick()
        self.assertEqual(self.used("b"), (0, 600))

    def test_changing_the_multiplier_applies_to_new_traffic(self):
        self.seed("b", [2])
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", down=1000)
        self.tick()
        self.set(2, 1.5)
        self.panel.add_traffic("b", down=1000)
        self.tick()
        self.assertEqual(self.used("b"), (0, 1200 + 1500))

    def test_remove_then_set_again_never_bills_the_gap(self):
        self.seed("b", [2])
        self.set(2, 1.2)
        self.tick()
        self.quiet(xm.op_remove, 2)                              # no tick in between: works with the daemon down
        self.panel.add_traffic("b", down=5000)
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", down=1000)
        self.tick()
        self.assertEqual(self.used("b"), (0, 5000 + 1200))

    def test_detach_and_reattach_never_bills_the_gap(self):
        self.seed("b", [1, 2])
        self.set(2, 1.2)
        self.tick()
        self.panel.detach("b", 2)
        self.tick()
        self.panel.add_traffic("b", down=5000)                   # direct only now: x1
        self.panel.attach("b", 2)
        self.tick()
        self.panel.add_traffic("b", down=1000)
        self.tick()
        self.assertEqual(self.used("b"), (0, 5000 + 1200))

    def test_deleted_client_is_forgotten(self):
        self.seed("b", [2])
        self.set(2, 1.2)
        self.tick()
        self.panel.delete_client("b")
        self.tick()
        self.assertEqual(self.q("SELECT COUNT(*) FROM xui_mult_ledger")[0][0], 0)

    def test_panel_enforces_the_quota_on_multiplied_usage(self):
        self.seed("b", [2], total=10_000)
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", down=9000)                   # x1.2 = 10800 >= 10000
        self.assertEqual(self.panel.disable_invalid(), [])
        self.tick()
        self.assertEqual(self.panel.disable_invalid(), ["b"])

    def test_dry_run_writes_nothing(self):
        self.seed("b", [2])
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", down=1000)
        with self.assertLogs("xui-mult", "INFO") as logs:
            self.tick(dry_run=True)
        self.assertIn("would bill 200 B", "\n".join(logs.output))
        self.assertEqual(self.used("b"), (0, 1000))

    def test_ledger_totals_accumulate(self):
        self.seed("b", [2])
        self.set(2, 1.5)
        self.tick()
        for _ in range(3):
            self.panel.add_traffic("b", up=100, down=300)
            self.tick()
        self.assertEqual(self.q("SELECT raw_total, extra_total FROM xui_mult_ledger WHERE email='b'")[0], (1200, 600))
        _, out = self.quiet(xm.op_list)
        self.assertIn("+600 B", out)


class SharedClientTests:
    """Several inbounds with the same clients (and the same multiplier): the client is billed ONCE, and every one
    of those inbounds is credited its share. (Regression: the lowest inbound id used to take everything, so
    `xui-mult list` and the logs showed +0 B on all the others.)"""
    N_IB, N_CLIENTS = 4, 250
    IDS = (2, 3, 4, 5)

    def setUp(self):
        super().setUp()
        self.add_inbounds(4, 5)
        self.emails = [f"s{i}" for i in range(self.N_CLIENTS)]
        self.penv.seed_many([(e, list(self.IDS), 0, 0) for e in self.emails])

    def configure(self, k, ids=IDS):
        for i in ids:
            self.set(i, k)

    def shares(self, ids=IDS):
        with xm.db_connect(self.db) as db:
            got = xm.extra_by_inbound(db, {i: xm.load_config()["inbounds"][i] for i in ids})
        return [got.get(i, 0) for i in ids]

    def list_rows(self):
        _, out = self.quiet(xm.op_list)
        return {i: next(line for line in out.splitlines() if re.match(rf"^\W*{i}\W", line)) for i in self.IDS}, out

    def test_client_is_billed_once_not_once_per_inbound(self):
        self.configure(1.2)
        self.tick()
        raw = {e: (5 * (i + 1), 10 * (i + 3)) for i, e in enumerate(self.emails)}
        self.penv.add_traffic_batch([(e, u, d) for e, (u, d) in raw.items()])
        self.tick()
        self.tick()                                                       # a repeat tick must not bill again
        for e, (u, d) in raw.items():
            self.assertEqual(self.used(e), (u + u // 5, d + d // 5), e)   # x1.2 exactly, never x1.2 per inbound

    def test_every_inbound_is_credited_with_its_share(self):
        self.configure(1.2)
        self.tick()
        self.penv.add_traffic_batch([(e, 5 * (i + 1), 10 * (i + 3)) for i, e in enumerate(self.emails)])
        self.tick()
        total = self.ledger_extra()
        got = self.shares()
        self.assertGreater(total, 0)
        self.assertEqual(sum(got), total, "the shares add up to exactly what the clients were billed")
        self.assertTrue(all(g > 0 for g in got), got)
        mean = total / self.N_IB
        self.assertTrue(all(abs(g - mean) <= 0.05 * mean for g in got), f"unbalanced shares {got}")

    def test_list_shows_no_inbound_starved_at_zero(self):
        self.configure(1.2)
        self.tick()
        self.penv.add_traffic_batch([(e, 0, 100_000) for e in self.emails])
        self.tick()
        rows, out = self.list_rows()
        for i, row in rows.items():
            self.assertIn("[1.20x]", row)
            self.assertNotIn("+0 B", row, f"inbound #{i} starved: {row}")
            self.assertIn(f"{self.N_CLIENTS}/{self.N_CLIENTS}", row)
        extras = {row.split("+", 1)[1].split("│")[0].strip() for row in rows.values()}
        self.assertLessEqual(len(extras), 2, f"shares should be (nearly) equal: {extras}")
        self.assertIn(f"+{xm.human(self.ledger_extra() // self.N_IB)}", out)

    def test_the_tick_log_reports_every_inbound(self):
        self.configure(1.2)
        self.tick()
        self.penv.add_traffic_batch([(e, 0, 100_000) for e in self.emails])
        with self.assertLogs("xui-mult", "INFO") as logs:
            self.tick()
        lines = [l for l in logs.output if " client(s) used " in l]
        self.assertEqual(sorted(int(l.split("#")[1].split()[0]) for l in lines), list(self.IDS))
        for l in lines:
            self.assertIn(f"{self.N_CLIENTS} client(s) used", l)
            self.assertNotIn("+0 B", l)
        self.assertEqual(self.status()["extra_since_start"], self.ledger_extra(), "no byte logged twice")
        self.assertEqual(self.status()["raw_since_start"], 100_000 * self.N_CLIENTS)

    def test_fractional_bytes_stay_balanced_and_carry_over(self):
        """3 bytes at x1.234 is 0.702 extra: fractions are carried per client, and what is attributed to the inbounds
        is the exact running total, never a rounded-per-tick approximation."""
        self.configure(1.234)
        db = xm.db_connect(self.db)
        cfg = {i: 1.234 for i in self.IDS}
        xm.bill(db, cfg)
        ticks = 100
        for _ in range(ticks):
            self.penv.add_traffic_batch([(e, 0, 3) for e in self.emails])
            xm.bill(db, cfg)
        raw = 3 * ticks
        want = raw * 234 // 1000                                          # 70 bytes: floor(300 x 0.234)
        for e in self.emails:
            self.assertEqual(self.used(e), (0, raw + want), e)
        self.assertEqual(self.ledger_extra(), want * self.N_CLIENTS)
        got = self.shares()
        self.assertEqual(sum(got), want * self.N_CLIENTS)
        self.assertLessEqual(max(got) - min(got), 0.1 * min(got), got)

    def test_per_tick_amounts_add_up_across_ticks(self):
        """What the log attributes tick after tick sums to the same totals the list shows."""
        self.configure(1.234)
        db = xm.db_connect(self.db)
        cfg = {i: 1.234 for i in self.IDS}
        xm.bill(db, cfg)
        raw, extra = dict.fromkeys(self.IDS, 0), dict.fromkeys(self.IDS, 0)
        for t in range(40):
            self.penv.add_traffic_batch([(e, 1, 2 + t % 3) for e in self.emails])
            per_ib, _ = xm.bill(db, cfg)
            for ib, (n, r, x) in per_ib.items():
                self.assertEqual(n, self.N_CLIENTS)
                self.assertGreaterEqual(x, 0)
                raw[ib] += r
                extra[ib] += x
        self.assertEqual([extra[i] for i in self.IDS], self.shares())
        self.assertEqual(sum(raw.values()), sum(1 + 2 + t % 3 for t in range(40)) * self.N_CLIENTS)

    def test_only_the_highest_multiplier_owns_the_client(self):
        self.set(2, 1.2)
        self.set(3, 1.5)
        self.set(4, 1.5)
        self.tick()
        self.penv.add_traffic_batch([(e, 0, 1000) for e in self.emails])
        self.tick()
        for e in self.emails[:3]:
            self.assertEqual(self.used(e), (0, 1500), "billed once, at the highest multiplier")
        got = dict(zip((2, 3, 4), self.shares((2, 3, 4))))
        self.assertEqual(got[2], 0, "the lower multiplier is not credited")
        self.assertEqual(got[3] + got[4], 500 * self.N_CLIENTS)
        self.assertLessEqual(abs(got[3] - got[4]), 1 + self.N_CLIENTS // 10)
        _, out = self.quiet(xm.op_list)
        self.assertIn("inbound #3: 250 client(s) are also on a lower-multiplier inbound", out)
        self.assertIn("inbound #4: 250 client(s) are also on a lower-multiplier inbound", out)

    def test_leaving_one_inbound_shifts_the_shares_without_losing_a_byte(self):
        self.configure(1.2)
        self.tick()
        self.penv.add_traffic_batch([(e, 0, 1000) for e in self.emails])
        self.tick()
        total = self.ledger_extra()
        for e in self.emails:
            self.panel.detach(e, 5)
        got = self.shares((2, 3, 4))
        self.assertEqual(sum(got), total, got)
        self.tick()
        self.penv.add_traffic_batch([(e, 0, 1000) for e in self.emails])
        self.tick()
        self.assertEqual(sum(self.shares((2, 3, 4))), self.ledger_extra())
        self.assertEqual(self.used(self.emails[0]), (0, 2400))

    def test_partly_overlapping_inbounds(self):
        """Half the clients on #2 only, half on #2 and #3: #2 owns its own, and shares the common ones with #3."""
        self.q("DELETE FROM client_inbounds WHERE inbound_id IN (3, 4, 5)")
        for e in self.emails[:125]:
            self.panel.attach(e, 3)
        self.configure(1.2, (2, 3))
        self.tick()
        self.penv.add_traffic_batch([(e, 0, 1000) for e in self.emails])
        self.tick()
        two, three = self.shares((2, 3))
        self.assertEqual(two + three, 200 * self.N_CLIENTS)
        # 125 clients only on #2 (200 extra bytes each), 125 on both (100 each side)
        self.assertEqual((two, three), (200 * 125 + 100 * 125, 100 * 125))


class RobustnessTests:
    def test_idle_ticks_write_nothing(self):
        self.seed("b", [2])
        db = xm.db_connect(self.db)
        xm.bill(db, {2: 1.2})
        marker = self.write_marker(db)
        for _ in range(200):
            self.assertEqual(xm.bill(db, {2: 1.2}), ({}, []))
        self.assertEqual(self.write_marker(db), marker)

    def test_interrupted_tick_is_atomic(self):
        self.seed("b", [2])
        db = xm.db_connect(self.db)
        xm.bill(db, {2: 1.2})
        self.panel.add_traffic("b", up=700, down=1000)
        self.break_ledger()
        with self.assertRaises(xm.DB_ERRORS):
            xm.bill(db, {2: 1.2})
        self.assertEqual(self.used("b"), (700, 1000), "credit rolled back with the ledger")
        self.fix_ledger()
        xm.bill(db, {2: 1.2})
        xm.bill(db, {2: 1.2})
        self.assertEqual(self.used("b"), (840, 1200), "billed exactly once")

    def test_process_killed_mid_transaction(self):
        self.seed("b", [2])
        db = xm.db_connect(self.db)
        xm.bill(db, {2: 1.2})
        self.panel.add_traffic("b", down=5000)
        script = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {os.path.dirname(HERE)!r})
            import xui_mult as xm
            db = xm.db_connect({self.db!r})
            real = db.ledger_update
            def dying(rows):
                real(rows)
                os._exit(9)            # credit and ledger written, no COMMIT, no ROLLBACK, no cleanup
            db.ledger_update = dying
            xm.bill(db, {{2: 1.2}})
        """)
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertEqual(self.used("b"), (0, 5000), "uncommitted credit vanished")
        xm.bill(db, {2: 1.2})
        self.assertEqual(self.used("b"), (0, 6000))

    def test_int64_overflow_saturates(self):
        self.seed("b", [2])
        db = xm.db_connect(self.db)
        xm.bill(db, {2: 10})
        self.panel.add_traffic("b", down=xm.TRAFFIC_MAX - 10)
        per_ib, _ = xm.bill(db, {2: 10})
        self.assertEqual(self.used("b")[1], xm.TRAFFIC_MAX)
        self.assertEqual(per_ib[2][1], xm.TRAFFIC_MAX - 10)
        self.panel.add_traffic("b", down=10 ** 15)                # already at the cap: stays there, nothing breaks
        xm.bill(db, {2: 10})
        self.assertLessEqual(self.used("b")[1], 2 ** 63 - 1)

    def test_read_only_connection_creates_nothing_and_writes_nothing(self):
        """What `preflight.sh --db` uses on a live production panel."""
        self.seed("b", [2])
        with xm.db_connect(self.db, ledger=False) as db:
            self.assertFalse(db.table_exists("xui_mult_ledger"), "the compatibility check must not create our table")
            self.assertEqual(db.rows("SELECT COUNT(*) FROM client_traffics")[0][0], 1)
            with self.assertRaises(xm.DB_ERRORS):
                db.run("UPDATE client_traffics SET up = 1")
        self.assertEqual(self.used("b"), (0, 0))

    def test_batch_size_does_not_change_the_statement_count(self):
        """A tick is a fixed handful of statements, whether 5 clients were billed or 300."""
        counts, seeded = [], 0
        db = xm.db_connect(self.db)
        for n in (5, 300):
            fresh = [f"u{i}" for i in range(seeded, n)]
            self.penv.seed_many([(e, [2], 0, 0) for e in fresh])
            seeded = n
            xm.bill(db, {2: 1.2})                                 # baseline for the new clients
            self.penv.add_traffic_batch([(f"u{i}", 100, 200) for i in range(n)])
            xm.bill(db, {2: 1.2})
            counts.append(db.last["statements"])
            self.assertEqual(db.last["clients"], n)
        self.assertEqual(counts[0], counts[1], counts)
        self.assertLessEqual(counts[1], 14)
        self.assertEqual(self.used("u299"), (120, 240))


class CliTests:
    def main(self, *argv):
        with mock.patch("os.geteuid", return_value=0):
            return self.quiet(xm.main, list(argv))

    def test_set_list_remove(self):
        self.seed("a", [1])
        self.seed("b", [2], enable=False)
        rc, out = self.main("set", "2", "1.2")
        self.assertEqual(rc, 0)
        self.assertIn("Germany Tunnel: 1.20x", out)
        self.assertEqual(xm.load_config()["inbounds"], {2: 1.2})
        rc, out = self.main("list")
        row = next(line for line in out.splitlines() if "Germany Tunnel" in line)
        self.assertIn("0/1", row)
        self.assertIn("[1.20x]", row)
        rc, out = self.main("remove", "2")
        self.assertEqual((rc, xm.load_config()["inbounds"]), (0, {}))

    def test_bad_input_is_refused(self):
        self.assertEqual(self.main("set", "99", "1.2")[0], 1)
        self.assertEqual(self.main("remove", "3")[0], 1)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.main("set", "2", "0.9")
        self.assertEqual(xm.load_config()["inbounds"], {})

    def test_status_reports_a_deleted_inbound(self):
        self.main("set", "3", "1.5")
        self.q("DELETE FROM inbounds WHERE id=3")
        rc, out = self.main("status")
        self.assertEqual(rc, 1)
        self.assertIn("Inbound #3 [1.50x] no longer exists", out)

    def test_menu(self):
        self.seed("b", [2])
        answers = iter(["1", "2", "1.3", "", "3", "2", "", "0"])
        with mock.patch("builtins.input", lambda *_: next(answers)):
            rc, out = self.quiet(xm.menu)
        self.assertEqual(rc, 0)
        self.assertIn("1.30x", out)
        self.assertIn("Inbound #2 is back to 1.00x", out)

    def test_db_command_shows_the_database_and_pins_it(self):
        rc, out = self.main("db")
        self.assertEqual(rc, 0)
        self.assertIn(self.kind, out)
        self.assertIn("3 inbound(s)", out)
        with mock.patch.dict(os.environ, self.panel_settings()):
            rc, out = self.main("db", "auto")
        self.assertEqual((rc, xm.load_config()["db"]), (0, "auto"))
        self.assertIn("panel settings", out)
        rc, out = self.main("db", "set", self.db)
        self.assertEqual((rc, xm.load_config()["db"]), (0, self.db))
        self.assertEqual(self.main("db", "set", "/nonexistent/x-ui.db")[0], 1)
        self.assertEqual(xm.load_config()["db"], self.db, "an unusable database is never pinned")

    def test_status_names_the_database(self):
        self.main("set", "2", "1.2")
        rc, out = self.main("status")
        self.assertIn("Database " + {"sqlite": "SQLite", "postgres": "PostgreSQL"}[self.kind], out)
        self.assertNotIn("secret", out, "the DSN password never reaches the screen")


class TestBillingSQLite(BillingTests, Base):
    pass


class TestBillingPostgres(BillingTests, PgBase):
    pass


class TestSharedClientsSQLite(SharedClientTests, Base):
    pass


class TestSharedClientsPostgres(SharedClientTests, PgBase):
    pass


class TestRobustnessSQLite(RobustnessTests, Base):
    pass


class TestRobustnessPostgres(RobustnessTests, PgBase):
    pass


class TestCliSQLite(CliTests, Base):
    pass


class TestCliPostgres(CliTests, PgBase):
    pass


# =================================================================================== SQLite only

class TestSQLite(Base):
    def test_concurrent_panel_writers_are_exact(self):
        """Panel writers (atomic adds + read-then-save inside IMMEDIATE transactions) race the daemon."""
        self.seed("a", [1])
        self.seed("b", [2])
        db = xm.db_connect(self.db)
        xm.bill(db, {2: 1.2})
        raw, stop = {"a": 0, "b": 0}, [False]

        def writer():
            c = self.penv.raw()
            while not stop[0]:
                e, n = random.choice("ab"), random.randint(1, 5000)
                c.execute("BEGIN IMMEDIATE")
                if random.random() < 0.5:
                    c.execute("UPDATE client_traffics SET down = down + ? WHERE email = ?", (n, e))
                else:
                    cur = c.execute("SELECT down FROM client_traffics WHERE email = ?", (e,)).fetchone()[0]
                    time.sleep(0.001)
                    c.execute("UPDATE client_traffics SET down = ? WHERE email = ?", (cur + n, e))
                c.execute("COMMIT")
                raw[e] += n
                time.sleep(0.002)

        ths = [threading.Thread(target=writer) for _ in range(2)]
        [t.start() for t in ths]
        ticks, t0 = 0, time.time()
        while time.time() - t0 < 3:
            xm.bill(db, {2: 1.2})
            ticks += 1
            time.sleep(0.003)
        stop[0] = True
        [t.join() for t in ths]
        xm.bill(db, {2: 1.2})
        self.assertGreater(ticks, 100)
        self.assertEqual(self.used("a")[1], raw["a"])
        self.assertEqual(self.used("b")[1], raw["b"] + raw["b"] * 200 // 1000)

    def test_unreadable_counters_skip_only_that_client(self):
        for e in ("bad", "neg", "ok"):
            self.seed(e, [2])
        self.set(2, 1.2)
        self.tick()
        self.q("UPDATE client_traffics SET down='garbage' WHERE email='bad'")
        self.q("UPDATE client_traffics SET up=-500 WHERE email='neg'")
        self.panel.add_traffic("ok", down=1000.0)
        with self.assertLogs("xui-mult", "WARNING"):
            self.tick()
        self.assertEqual(self.used("ok"), (0, 1200))
        self.assertEqual(self.status()["skipped"], ["bad"])

    def test_locked_database_skips_the_tick_then_bills_once(self):
        self.seed("b", [2])
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", down=1000)
        xm.BUSY_TIMEOUT_MS = 300
        holder = sqlite3.connect(self.db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")                        # the panel inside a long write
        try:
            t0 = time.time()
            with self.assertLogs("xui-mult", "WARNING"):
                self.tick()
            self.assertLess(time.time() - t0, 5)
            self.assertTrue(self.status()["db_busy"])
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.tick()
        self.tick()
        self.assertEqual(self.used("b"), (0, 1200))

    def test_database_replaced_reconnects(self):
        self.seed("b", [2])
        self.set(2, 1.2)

        def script(wait):
            wait(2)
            shutil.copy(self.db, self.db + ".bak")
            os.replace(self.db + ".bak", self.db)                 # e.g. the panel's "restore backup"
            wait(2)
            self.panel.add_traffic("b", down=500)
            wait(3)
        with self.assertLogs("xui-mult", "WARNING") as logs:
            self.run_daemon_with(script)
        self.assertIn("reconnecting", "\n".join(logs.output))
        self.assertEqual(self.used("b"), (0, 600))

    def test_missing_database_is_reported_not_fatal(self):
        os.unlink(self.db)
        with self.assertLogs("xui-mult", "ERROR"):
            self.tick()
        self.assertIn("database not found", self.status()["db_error"])


# =================================================================================== PostgreSQL only

class TestPostgres(PgBase):
    def test_both_dsn_forms_connect(self):
        c = pgcluster.get()
        for form in ("url", "kv"):
            with xm.db_connect(c.dsn(self.penv.name, form)) as db:
                self.assertEqual(db.kind, "postgres")
                self.assertIn("PostgreSQL", db.describe())

    def test_not_a_panel_database_is_refused(self):
        c = pgcluster.get()
        empty = c.new_database()
        try:
            with self.assertRaisesRegex(xm.XMError, "table 'client_traffics' missing"):
                xm.db_connect(c.dsn(empty))
        finally:
            c.drop_database(empty)

    def test_missing_driver_says_how_to_install_it(self):
        with mock.patch.object(xm, "pg", None):
            with self.assertRaisesRegex(xm.XMError, "python3-psycopg2"):
                xm.db_connect(self.db)

    def test_unreachable_server_never_leaks_the_password(self):
        dsn = "postgres://xui:hunter2@127.0.0.1:1/xui?sslmode=disable"
        with self.assertRaises(xm.XMError) as cm:
            xm.db_connect(dsn)
        self.assertNotIn("hunter2", str(cm.exception))

    def test_counters_far_beyond_the_cap_do_not_overflow_bigint(self):
        """PostgreSQL raises on bigint overflow (SQLite quietly turns it into REAL): the credit is clamped first."""
        self.seed("b", [2], up=2 ** 63 - 5, down=xm.TRAFFIC_MAX - 5)
        self.set(2, 10)
        self.tick()
        self.panel.add_traffic("b", up=1, down=3)
        self.tick()
        self.assertEqual(self.status()["error"], "")
        up, down = self.used("b")
        self.assertLessEqual(up, 2 ** 63 - 1)
        self.assertEqual(down, xm.TRAFFIC_MAX)

    def test_negative_counters_count_as_zero(self):
        self.seed("neg", [2])
        self.set(2, 1.2)
        self.tick()
        self.q("UPDATE client_traffics SET up = -500 WHERE email='neg'")
        self.tick()
        self.assertEqual(self.status()["error"], "")

    def test_a_row_the_panel_holds_is_skipped_not_waited_for(self):
        for e in ("held", "free"):
            self.seed(e, [2])
        self.set(2, 1.2)
        self.tick()
        for e in ("held", "free"):
            self.panel.add_traffic(e, down=1000)
        holder = self.penv.raw(autocommit=False)                  # the panel, mid-transaction, on one client's row
        holder.cursor().execute("UPDATE client_traffics SET last_online = 1 WHERE email = 'held'")
        try:
            t0 = time.time()
            self.tick()
            self.assertLess(time.time() - t0, 3, "a locked row must not stall the tick")
            self.assertEqual(self.used("free"), (0, 1200))
            self.assertEqual(self.used("held"), (0, 1000), "skipped this tick")
            self.assertEqual(self.status()["error"], "")
        finally:
            holder.rollback()
            holder.close()
        self.tick()
        self.assertEqual(self.used("held"), (0, 1200), "billed in full on the next tick")

    def test_panel_writers_in_any_order_never_deadlock_with_us(self):
        """The panel updates its rows in slice order, which varies; we must not form a lock cycle with it."""
        names = [f"c{i}" for i in range(120)]
        self.penv.seed_many([(n, [2], 0, 0) for n in names])
        db = xm.db_connect(self.db)
        xm.bill(db, {2: 1.2})
        raw, errors, stop = {n: 0 for n in names}, [], [False]
        serial = threading.Lock()      # the panel funnels its traffic writes through one serial writer

        def writer():
            while not stop[0]:
                batch = [(n, 0, random.randint(1, 4000)) for n in random.sample(names, 60)]
                try:
                    with serial:
                        self.panel.add_traffic_batch(batch, shuffle=True, pause=0.0003)
                        for n, _, d in batch:
                            raw[n] += d
                except Exception as e:  # noqa: BLE001 — a deadlock victim would land here
                    errors.append(e)
                    return

        ths = [threading.Thread(target=writer) for _ in range(3)]
        [t.start() for t in ths]
        t0, ticks = time.time(), 0
        while time.time() - t0 < 4:
            xm.bill(db, {2: 1.2})
            ticks += 1
            time.sleep(0.005)
        stop[0] = True
        [t.join() for t in ths]
        self.assertEqual(errors, [], "the panel was a deadlock victim")
        for _ in range(3):
            xm.bill(db, {2: 1.2})
        self.assertGreater(ticks, 20)
        for n in names:
            self.assertEqual(self.used(n)[1], raw[n] + raw[n] * 200 // 1000, n)

    def test_two_billers_at_once_never_double_bill(self):
        """The daemon and a CLI `remove` (or a second daemon by mistake) must serialise on the advisory lock."""
        names = [f"c{i}" for i in range(80)]
        self.penv.seed_many([(n, [2], 0, 0) for n in names])
        xm.bill(xm.db_connect(self.db), {2: 1.2})
        raw, stop = {n: 0 for n in names}, [False]

        def biller():
            db = xm.db_connect(self.db)
            while not stop[0]:
                xm.bill(db, {2: 1.2})
            db.close()

        ths = [threading.Thread(target=biller) for _ in range(3)]
        [t.start() for t in ths]
        for _ in range(25):
            batch = [(n, 0, random.randint(1, 3000)) for n in names]
            self.panel.add_traffic_batch(batch)
            for n, _, d in batch:
                raw[n] += d
            time.sleep(0.02)
        stop[0] = True
        [t.join() for t in ths]
        xm.bill(xm.db_connect(self.db), {2: 1.2})
        for n in names:
            self.assertEqual(self.used(n)[1], raw[n] + raw[n] * 200 // 1000, n)

    def test_lock_timeout_is_a_busy_tick_not_a_crash(self):
        self.seed("b", [2])
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", down=1000)
        holder = self.penv.raw()
        holder.cursor().execute("SELECT pg_advisory_lock(%s)", (xm.PG_ADVISORY_KEY,))   # another xui-mult mid-tick
        try:
            with mock.patch.object(xm, "PG_LOCK_TIMEOUT_S", 1), self.assertLogs("xui-mult", "WARNING"):
                t0 = time.time()
                self.tick()
            self.assertLess(time.time() - t0, 6)
            self.assertTrue(self.status()["db_busy"])
            self.assertEqual(self.used("b"), (0, 1000))
        finally:
            holder.close()
        self.tick()
        self.assertEqual(self.used("b"), (0, 1200))

    def test_idle_in_transaction_session_cannot_hold_panel_rows_forever(self):
        with xm.db_connect(self.db) as db:
            self.assertEqual(db.rows("SHOW idle_in_transaction_session_timeout")[0][0], "1min")
            self.assertEqual(db.rows("SHOW lock_timeout")[0][0], "5s")

    def test_ledger_creation_is_race_free(self):
        self.q("DROP TABLE IF EXISTS xui_mult_ledger")
        errors = []

        def connect():
            try:
                xm.db_connect(self.db).close()
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ths = [threading.Thread(target=connect) for _ in range(6)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        self.assertEqual(errors, [])

    def test_daemon_reconnects_after_the_connection_is_killed(self):
        self.seed("b", [2])
        self.set(2, 1.2)

        def script(wait):
            wait(2)
            self.q("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'xui-mult'")
            wait(2)
            self.panel.add_traffic("b", down=1000)
            wait(3)
        with self.assertLogs("xui-mult", "ERROR"):
            self.run_daemon_with(script)
        self.assertEqual(self.used("b"), (0, 1200))

    def test_daemon_survives_a_postgresql_restart(self):
        self.seed("b", [2])
        self.set(2, 1.2)

        def script(wait):
            wait(2)
            pgcluster.get().restart()
            wait(3)
            self.panel.add_traffic("b", down=1000)
            wait(3)
        with self.assertLogs("xui-mult", "ERROR"):
            self.run_daemon_with(script)
        self.assertEqual(self.used("b"), (0, 1200))
        self.assertEqual(self.status()["error"], "")

    def test_daemon_follows_a_migration_from_sqlite_to_postgres(self):
        """`x-ui migrate-db`: the panel's env file starts saying postgres; no restart of xui-mult needed."""
        os.makedirs(self.tmp + "/lite")
        lite = mp.SqliteEnv(self.tmp + "/lite")
        for env in (lite, self.penv):
            env.seed("m", [2])
        envfile = os.path.join(self.tmp, "x-ui.env")
        with open(envfile, "w") as f:
            f.write(f"XUI_DB_FOLDER={self.tmp}/lite\n")
        with open(xm.CONF_PATH, "w") as f:
            json.dump(dict(xm.DEFAULT_CONFIG, db="auto", inbounds={"2": 1.2}), f)

        def script(wait):
            wait(2)
            lite.panel.add_traffic("m", down=1000)
            wait(2)
            with open(envfile, "w") as f:
                f.write(f"XUI_DB_TYPE=postgres\nXUI_DB_DSN={self.db}\n")
            wait(2)
            self.panel.add_traffic("m", down=1000)
            wait(3)
        with mock.patch.object(xm, "PANEL_ENV_FILES", (envfile,)), self.assertLogs("xui-mult", "WARNING"):
            self.run_daemon_with(script)
        self.assertEqual(lite.used("m"), (0, 1200))
        self.assertEqual(self.used("m"), (0, 1200))


# =================================================================================== choosing the database

class TestSplitShare(unittest.TestCase):
    def test_shares_always_add_up_exactly(self):
        for n in range(1, 9):
            ties = tuple(range(10, 10 + n))
            for total in list(range(0, 200)) + [10 ** 6 + 3, 2 ** 40 + 7, xm.TRAFFIC_MAX]:
                got = xm.split_share(total, ties, "someone@example")
                self.assertEqual(sum(got.values()), total, (n, total))
                self.assertLessEqual(max(got.values()) - min(got.values()), 1)

    def test_a_share_never_shrinks_as_the_total_grows(self):
        ties = (2, 3, 4, 5)
        prev = xm.split_share(0, ties, "x")
        for total in range(1, 400):
            cur = xm.split_share(total, ties, "x")
            self.assertTrue(all(cur[i] >= prev[i] for i in ties), total)
            prev = cur

    def test_remainder_bytes_rotate_between_clients(self):
        ties, got = (2, 3, 4, 5), dict.fromkeys((2, 3, 4, 5), 0)
        for i in range(2000):
            for ib, v in xm.split_share(1, ties, f"client{i}").items():   # one byte each: who gets it?
                got[ib] += v
        self.assertEqual(sum(got.values()), 2000)
        self.assertTrue(all(400 <= v <= 600 for v in got.values()), got)

    def test_single_owner_gets_everything(self):
        self.assertEqual(xm.split_share(12345, (7,), "a"), {7: 12345})


class TestTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.envfile = os.path.join(self.tmp, "x-ui")
        self.patches = [mock.patch.object(xm, "PANEL_ENV_FILES", (self.envfile,)),
                        mock.patch.dict(os.environ, {}, clear=False)]
        for k in ("XUI_DB_TYPE", "XUI_DB_DSN", "XUI_DB_FOLDER", "XUI_MULT_DB"):
            os.environ.pop(k, None)
        [p.start() for p in self.patches]

    def tearDown(self):
        [p.stop() for p in self.patches]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, text):
        with open(self.envfile, "w") as f:
            f.write(text)

    def test_default_is_the_panels_sqlite_file(self):
        t = xm.resolve_target({"db": "auto"})
        self.assertEqual((t.kind, t.ref), ("sqlite", xm.DEFAULT_DB))

    def test_legacy_config_with_the_default_path_still_follows_the_panel(self):
        self.env("XUI_DB_TYPE=postgres\nXUI_DB_DSN=postgres://u:p@h/d\n")
        self.assertEqual(xm.resolve_target({"db": xm.DEFAULT_DB}).kind, "postgres")

    def test_panel_env_file_selects_postgres(self):
        self.env("# comment\nXUI_DB_TYPE=postgres\nXUI_DB_DSN=postgres://xui:p%40ss@db.local:5433/xui?sslmode=disable\n")
        t = xm.resolve_target({"db": "auto"})
        self.assertEqual((t.kind, t.ref), ("postgres", "postgres://xui:p%40ss@db.local:5433/xui?sslmode=disable"))

    def test_env_file_syntax(self):
        self.env("export XUI_DB_TYPE='PostgreSQL'\nXUI_DB_DSN=\"host=/run/postgresql user=xui dbname=xui\"\n")
        t = xm.resolve_target({"db": "auto"})
        self.assertEqual((t.kind, t.ref), ("postgres", "host=/run/postgresql user=xui dbname=xui"))

    def test_sqlite_folder_from_the_panel_env(self):
        self.env("XUI_DB_FOLDER=/data/xui\n")
        self.assertEqual(xm.resolve_target({"db": "auto"}).ref, "/data/xui/x-ui.db")

    def test_postgres_without_a_dsn_is_an_error(self):
        self.env("XUI_DB_TYPE=postgres\n")
        with self.assertRaisesRegex(xm.XMError, "XUI_DB_DSN is empty"):
            xm.resolve_target({"db": "auto"})

    def test_pinned_config_wins_over_the_panel(self):
        self.env("XUI_DB_TYPE=postgres\nXUI_DB_DSN=postgres://u:p@h/d\n")
        self.assertEqual(xm.resolve_target({"db": "/srv/other.db"}).kind, "sqlite")
        self.assertEqual(xm.resolve_target({"db": "host=a dbname=b"}).kind, "postgres")

    def test_xui_mult_db_env_wins_over_everything(self):
        with mock.patch.dict(os.environ, {"XUI_MULT_DB": "/tmp/x.db"}):
            self.assertEqual(xm.resolve_target({"db": "host=a dbname=b"}).ref, "/tmp/x.db")

    def test_our_own_environment_overrides_the_env_file(self):
        self.env("XUI_DB_TYPE=sqlite\n")
        with mock.patch.dict(os.environ, {"XUI_DB_TYPE": "postgres", "XUI_DB_DSN": "postgres://u:p@h/d"}):
            self.assertEqual(xm.resolve_target({"db": "auto"}).kind, "postgres")

    def test_dsn_or_path(self):
        for ref, dsn in [("postgres://u@h/d", True), ("postgresql://u@h/d", True), ("host=h dbname=d", True),
                         ("/etc/x-ui/x-ui.db", False), ("/srv/a=b/x.db", False), ("x-ui.db", False)]:
            self.assertEqual(xm.is_dsn(ref), dsn, ref)

    def test_passwords_are_masked(self):
        for raw in ("postgres://xui:hunter2@h:5432/d?sslmode=disable", "host=h password=hunter2 dbname=d",
                    "host=h password='hunter 2' dbname=d", "postgres://xui@h/d?password=hunter2&sslmode=disable"):
            self.assertNotIn("hunter", xm.mask_dsn(raw), raw)
        self.assertIn("xui:***@h", xm.mask_dsn("postgres://xui:hunter2@h/d"))


class TestTerminalUi(Base):
    REMARKS = ["Direct", "🇩🇪 Germany Tunnel", "تانل ترکیه‌ای 🇹🇷", "日本 Tokyo relay", "café ❤️ 👨‍👩‍👧"]

    def boxed_widths(self, text):
        lines = [l for l in text.splitlines() if xm.ANSI_RE.sub("", l).lstrip(xm.LRM)[:1] in "╭│├╰"]
        self.assertTrue(lines)
        return {xm.disp_width(l) for l in lines}

    def test_display_width(self):
        for s, w in [("abc", 3), ("🇩🇪", 2), ("日本", 4), ("\033[96mab\033[0m", 2), ("e\u0301", 1), ("❤️", 2),
                     ("👨‍👩‍👧", 2), ("●", 1), ("می‌شود", 5), ("سلام", 4), ("ب\u064e", 1)]:
            self.assertEqual(xm.disp_width(s), w, repr(s))

    def test_table_stays_aligned_with_emoji_persian_and_cjk(self):
        rows = [[(str(i), None), (r, xm.C.title), ("vless:443", None)] for i, r in enumerate(self.REMARKS)]
        for colour in (False, True):
            xm.C.on = colour
            self.assertEqual(len(self.boxed_widths(xm.table(["ID", "REMARK", "PORT"], rows))), 1, colour)

    def test_narrow_terminal(self):
        rows = [[(str(i), None), (r * 3, None), ("shadowsocks:8388", None)] for i, r in enumerate(self.REMARKS)]
        with mock.patch.dict(os.environ, {"COLUMNS": "60"}):
            (w,) = self.boxed_widths(xm.table(["ID", "REMARK", "PROTOCOL:PORT"], rows, shrink=(1, 2)))
            self.assertLessEqual(w, 60)
            self.assertEqual(len(self.boxed_widths(xm.card("t", ["x" * 200, xm.SEP]))), 1)

    def test_list_badges(self):
        self.q("UPDATE inbounds SET enable=?, remark=? WHERE id=3", (False, "🇹🇷 Tunnel 2"))
        self.set(2, 1.2)
        _, out = self.quiet(xm.op_list)
        self.assertEqual(len(self.boxed_widths(out)), 1)
        self.assertIn("[DISABLED] 🇹🇷 Tunnel 2", out)
        self.assertIn("[1.20x]", out)
        self.assertIn("1.00x", out)

    def test_menu_rejects_bad_input_and_asks_again(self):
        answers = iter(["1", "99", "2", "0.5", "1.25", "", "0"])
        with mock.patch("builtins.input", lambda *_: next(answers)):
            rc, out = self.quiet(xm.menu)
        self.assertEqual(rc, 0)
        self.assertIn("no inbound #99", out)
        self.assertIn("multiplier must be above 1.0", out)
        self.assertEqual(xm.load_config()["inbounds"], {2: 1.25})
        self.assertEqual(len(self.boxed_widths(out)), 2, "dashboard card + inbound table")


if __name__ == "__main__":
    unittest.main(verbosity=2)
