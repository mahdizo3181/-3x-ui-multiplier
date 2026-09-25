import contextlib
import io
import json
import os
import random
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


class Base(unittest.TestCase):
    """Inbounds: #1 Direct, #2 Germany Tunnel, #3 Tunnel 2 (see mock_panel.create_db)."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"COLUMNS": "120"})   # deterministic wrapping
        self.env.start()
        self.tmp = tempfile.mkdtemp()
        xm.CONF_DIR = os.path.join(self.tmp, "etc")
        xm.RUN_DIR = os.path.join(self.tmp, "run")
        xm.CONF_PATH = os.path.join(xm.CONF_DIR, "config.json")
        xm.STATUS_PATH = os.path.join(xm.RUN_DIR, "status.json")
        xm.C.on = False
        self._busy = xm.BUSY_TIMEOUT_MS
        self.db = os.path.join(self.tmp, "x-ui.db")
        mp.create_db(self.db)
        self.panel = mp.Panel(self.db)
        os.makedirs(xm.CONF_DIR)
        with open(xm.CONF_PATH, "w") as f:
            json.dump(dict(xm.DEFAULT_CONFIG, db=self.db), f)

    def tearDown(self):
        self.env.stop()
        xm.C.on = False
        xm.BUSY_TIMEOUT_MS = self._busy
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, sql, args=()):
        c = sqlite3.connect(self.db)
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    def used(self, email):
        r = self.q("SELECT up, down FROM client_traffics WHERE email=?", (email,))
        return r[0] if r else None

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


class TestBilling(Base):
    def test_tunnel_client_pays_k(self):
        mp.seed_client(self.db, "b", [2])
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", up=1000, down=5000)
        self.tick()
        self.assertEqual(self.used("b"), (1200, 6000))
        self.tick()
        self.assertEqual(self.used("b"), (1200, 6000), "our own credit is never billed again")

    def test_other_inbounds_untouched(self):
        mp.seed_client(self.db, "a", [1])
        mp.seed_client(self.db, "c", [3])
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("a", down=1000)
        self.panel.add_traffic("c", down=1000)
        self.tick()
        self.assertEqual(self.used("a"), (0, 1000))
        self.assertEqual(self.used("c"), (0, 1000))

    def test_membership_comes_from_client_inbounds_not_the_stale_pointer(self):
        mp.seed_client(self.db, "m", [1, 2])      # client_traffics.inbound_id says 1
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("m", down=1000)
        self.tick()
        self.assertEqual(self.used("m"), (0, 1200))
        _, out = self.quiet(xm.op_list)
        self.assertIn("inbound #2: 1 client(s) are also on a lower-multiplier inbound", out)

    def test_highest_multiplier_wins(self):
        mp.seed_client(self.db, "d", [2, 3])
        self.set(2, 1.2)
        self.set(3, 1.5)
        self.tick()
        self.panel.add_traffic("d", down=1000)
        self.tick()
        self.assertEqual(self.used("d"), (0, 1500))

    def test_new_clients_are_picked_up_and_history_is_never_billed(self):
        mp.seed_client(self.db, "old", [2], down=10_000)        # used before the multiplier was set
        self.set(2, 1.2)
        self.tick()
        self.assertEqual(self.used("old"), (0, 10_000))
        mp.seed_client(self.db, "new", [2])                      # created later in the panel
        self.tick()
        for e in ("old", "new"):
            self.panel.add_traffic(e, down=1000)
        self.tick()
        self.assertEqual(self.used("old"), (0, 11_200))
        self.assertEqual(self.used("new"), (0, 1200))

    def test_fractions_carry_over(self):
        mp.seed_client(self.db, "b", [2])
        self.set(2, 1.234)
        conn = xm.db_connect(self.db)
        xm.bill(conn, {2: 1.234})
        for _ in range(1000):
            self.panel.add_traffic("b", down=3)                  # 3 x 0.234 = 0.702 extra per tick
            xm.bill(conn, {2: 1.234})
        self.assertEqual(self.used("b"), (0, 3000 + 3000 * 234 // 1000))

    def test_concurrent_panel_writers_are_exact(self):
        """Panel writers (atomic adds + read-then-save inside IMMEDIATE transactions) race the daemon."""
        mp.seed_client(self.db, "a", [1])
        mp.seed_client(self.db, "b", [2])
        conn = xm.db_connect(self.db)
        xm.bill(conn, {2: 1.2})
        raw, stop = {"a": 0, "b": 0}, [False]

        def writer():
            c = sqlite3.connect(self.db, timeout=10, isolation_level=None)
            c.execute("PRAGMA busy_timeout=10000")
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
            xm.bill(conn, {2: 1.2})
            ticks += 1
            time.sleep(0.003)
        stop[0] = True
        [t.join() for t in ths]
        xm.bill(conn, {2: 1.2})
        self.assertGreater(ticks, 100)
        self.assertEqual(self.used("a")[1], raw["a"])
        self.assertEqual(self.used("b")[1], raw["b"] + raw["b"] * 200 // 1000)

    def test_panel_reset_is_followed(self):
        mp.seed_client(self.db, "b", [2])
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
        mp.seed_client(self.db, "b", [2])
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", down=1000)
        self.tick()
        self.set(2, 1.5)
        self.panel.add_traffic("b", down=1000)
        self.tick()
        self.assertEqual(self.used("b"), (0, 1200 + 1500))

    def test_remove_then_set_again_never_bills_the_gap(self):
        mp.seed_client(self.db, "b", [2])
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
        mp.seed_client(self.db, "b", [1, 2])
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
        mp.seed_client(self.db, "b", [2])
        self.set(2, 1.2)
        self.tick()
        self.panel.delete_client("b")
        self.tick()
        self.assertEqual(self.q("SELECT COUNT(*) FROM xui_mult_ledger")[0][0], 0)

    def test_panel_enforces_the_quota_on_multiplied_usage(self):
        mp.seed_client(self.db, "b", [2], total=10_000)
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", down=9000)                   # x1.2 = 10800 >= 10000
        self.assertEqual(self.panel.disable_invalid(), [])
        self.tick()
        self.assertEqual(self.panel.disable_invalid(), ["b"])

    def test_dry_run_writes_nothing(self):
        mp.seed_client(self.db, "b", [2])
        self.set(2, 1.2)
        self.tick()
        self.panel.add_traffic("b", down=1000)
        with self.assertLogs("xui-mult", "INFO") as logs:
            self.tick(dry_run=True)
        self.assertIn("would bill 200 B", "\n".join(logs.output))
        self.assertEqual(self.used("b"), (0, 1000))


class TestRobustness(Base):
    def test_idle_ticks_write_nothing(self):
        mp.seed_client(self.db, "b", [2])
        conn = xm.db_connect(self.db)
        xm.bill(conn, {2: 1.2})
        writes = conn.total_changes
        for _ in range(200):
            self.assertEqual(xm.bill(conn, {2: 1.2}), ({}, []))
        self.assertEqual(conn.total_changes, writes)

    def test_interrupted_tick_is_atomic(self):
        mp.seed_client(self.db, "b", [2])
        conn = xm.db_connect(self.db)
        xm.bill(conn, {2: 1.2})
        self.panel.add_traffic("b", up=700, down=1000)
        conn.execute("CREATE TRIGGER boom BEFORE UPDATE ON xui_mult_ledger "
                     "BEGIN SELECT RAISE(ABORT, 'simulated crash'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            xm.bill(conn, {2: 1.2})
        self.assertEqual(self.used("b"), (700, 1000), "credit rolled back with the ledger")
        conn.execute("DROP TRIGGER boom")
        xm.bill(conn, {2: 1.2})
        xm.bill(conn, {2: 1.2})
        self.assertEqual(self.used("b"), (840, 1200), "billed exactly once")

    def test_process_killed_mid_transaction(self):
        mp.seed_client(self.db, "b", [2])
        conn = xm.db_connect(self.db)
        xm.bill(conn, {2: 1.2})
        self.panel.add_traffic("b", down=5000)
        script = textwrap.dedent(f"""
            import os, sqlite3, sys
            sys.path.insert(0, {os.path.dirname(HERE)!r})
            import xui_mult as xm
            class Dying(sqlite3.Connection):
                def executemany(self, sql, *a):
                    r = super().executemany(sql, *a)
                    if sql.startswith("UPDATE client_traffics"):
                        os._exit(9)            # no COMMIT, no ROLLBACK, no cleanup
                    return r
            c = sqlite3.connect({self.db!r}, isolation_level=None, factory=Dying)
            xm.bill(c, {{2: 1.2}})
        """)
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertEqual(self.used("b"), (0, 5000), "uncommitted credit vanished")
        xm.bill(conn, {2: 1.2})
        self.assertEqual(self.used("b"), (0, 6000))

    def test_unreadable_counters_skip_only_that_client(self):
        for e in ("bad", "neg", "ok"):
            mp.seed_client(self.db, e, [2])
        self.set(2, 1.2)
        self.tick()
        c = sqlite3.connect(self.db)
        c.execute("UPDATE client_traffics SET down='garbage' WHERE email='bad'")
        c.execute("UPDATE client_traffics SET up=-500 WHERE email='neg'")
        c.commit()
        c.close()
        self.panel.add_traffic("ok", down=1000.0)
        with self.assertLogs("xui-mult", "WARNING"):
            self.tick()
        self.assertEqual(self.used("ok"), (0, 1200))
        self.assertEqual(self.status()["skipped"], ["bad"])

    def test_int64_overflow_saturates(self):
        mp.seed_client(self.db, "b", [2])
        conn = xm.db_connect(self.db)
        xm.bill(conn, {2: 10})
        self.panel.add_traffic("b", down=xm.TRAFFIC_MAX - 10)
        per_ib, _ = xm.bill(conn, {2: 10})
        self.assertEqual(self.used("b")[1], xm.TRAFFIC_MAX)
        self.assertEqual(per_ib[2][1], xm.TRAFFIC_MAX - 10)

    def test_locked_database_skips_the_tick_then_bills_once(self):
        mp.seed_client(self.db, "b", [2])
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
        mp.seed_client(self.db, "b", [2])
        self.set(2, 1.2)
        self.tick()
        shutil.copy(self.db, self.db + ".bak")
        os.replace(self.db + ".bak", self.db)                     # e.g. the panel's "restore backup"
        self.panel.add_traffic("b", down=500)
        self.tick()
        self.assertEqual(self.used("b"), (0, 600))


class TestCli(Base):
    def main(self, *argv):
        with mock.patch("os.geteuid", return_value=0):
            return self.quiet(xm.main, list(argv))

    def test_set_list_remove(self):
        mp.seed_client(self.db, "a", [1])
        mp.seed_client(self.db, "b", [2], enable=False)
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
        c = sqlite3.connect(self.db)
        c.execute("DELETE FROM inbounds WHERE id=3")
        c.commit()
        c.close()
        rc, out = self.main("status")
        self.assertEqual(rc, 1)
        self.assertIn("Inbound #3 [1.50x] no longer exists", out)

    def test_menu(self):
        mp.seed_client(self.db, "b", [2])
        answers = iter(["1", "2", "1.3", "", "3", "2", "", "0"])
        with mock.patch("builtins.input", lambda *_: next(answers)):
            rc, out = self.quiet(xm.menu)
        self.assertEqual(rc, 0)
        self.assertIn("1.30x", out)
        self.assertIn("Inbound #2 is back to 1.00x", out)


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
        c = sqlite3.connect(self.db)
        c.execute("UPDATE inbounds SET enable=0, remark='🇹🇷 Tunnel 2' WHERE id=3")
        c.commit()
        c.close()
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
