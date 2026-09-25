"""Edge cases: interrupted/killed billing, zero ticks, negative/overflowing counters, a locked database,
one bad pair, API outages/auth/scope/domain, and the lifecycle rules (fail closed, first use via tunnel)."""
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

from test_xui_mult import Base, GB, mp, xm

DAY = 86400000


def status():
    with open(xm.STATUS_PATH) as f:
        return json.load(f)


class TestBillingEdges(Base):
    def pair(self, **kw):
        mp.seed_client(self.db, "u1", [1], **kw)
        mp.seed_client(self.db, "u1_tun", [2])
        conn = xm.db_connect(self.db)
        xm.ledger_init(conn, "u1_tun")
        return conn

    def test_zero_byte_ticks_write_nothing(self):
        conn = self.pair()
        before = xm.ledger_row(conn, "u1_tun")
        writes = conn.total_changes
        for _ in range(200):
            self.assertEqual(xm.account(conn, "u1", "u1_tun", 1200), (0, 0))
        self.assertEqual(conn.total_changes, writes, "an idle tick must not write")
        self.assertEqual(xm.ledger_row(conn, "u1_tun"), before)
        self.assertEqual(self.traffic("u1")[:2], (0, 0))

    def test_interrupted_transfer_is_atomic(self):
        """Credit and ledger commit together: a failure between them leaves both untouched."""
        conn = self.pair()
        self.panel.add_traffic("u1_tun", up=700, down=1000)
        conn.execute("CREATE TRIGGER boom BEFORE UPDATE ON tunnel_multiplier_ledger "
                     "WHEN NEW.raw_total > OLD.raw_total BEGIN SELECT RAISE(ABORT, 'simulated crash'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            xm.account(conn, "u1", "u1_tun", 1200)
        self.assertEqual(self.traffic("u1")[:2], (0, 0), "master credit rolled back")
        self.assertEqual(xm.ledger_row(conn, "u1_tun")["last_down"], 0)
        conn.execute("DROP TRIGGER boom")
        self.assertEqual(xm.account(conn, "u1", "u1_tun", 1200), (1700, 2040))
        self.assertEqual(xm.account(conn, "u1", "u1_tun", 1200), (0, 0))
        self.assertEqual(self.traffic("u1")[:2], (840, 1200), "billed exactly once")

    def test_process_killed_mid_transaction(self):
        """SIGKILL-like exit right after the master UPDATE, before COMMIT: SQLite rolls it back."""
        conn = self.pair()
        self.panel.add_traffic("u1_tun", down=5000)
        script = textwrap.dedent(f"""
            import os, sqlite3, sys
            sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
            import xui_mult as xm
            class Dying(sqlite3.Connection):
                def execute(self, sql, *a):
                    r = super().execute(sql, *a)
                    if sql.startswith("UPDATE client_traffics"):
                        os._exit(9)            # no COMMIT, no ROLLBACK, no cleanup
                    return r
            c = sqlite3.connect({self.db!r}, isolation_level=None, factory=Dying)
            xm.account(c, "u1", "u1_tun", 1200)
        """)
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertEqual(self.traffic("u1")[1], 0, "uncommitted credit must vanish")
        self.assertEqual(xm.ledger_row(conn, "u1_tun")["last_down"], 0)
        self.assertEqual(xm.account(conn, "u1", "u1_tun", 1200), (5000, 6000))
        self.assertEqual(self.traffic("u1")[1], 6000)

    def test_negative_and_non_integer_counters(self):
        conn = self.pair(up=-300)                 # master negative: still credited arithmetically
        c = sqlite3.connect(self.db)
        c.execute("UPDATE client_traffics SET up=-500, down=2500.0 WHERE email='u1_tun'")  # garbage shadow row
        c.commit()
        c.close()
        self.assertEqual(xm.account(conn, "u1", "u1_tun", 1000), (2500, 2500))
        led = xm.ledger_row(conn, "u1_tun")
        self.assertEqual((led["last_up"], led["last_down"]), (0, 2500))
        self.assertIsInstance(led["last_down"], int)
        self.assertEqual(self.traffic("u1")[:2], (-300, 2500))

    def test_int64_overflow_is_clamped(self):
        conn = self.pair(down=xm.TRAFFIC_MAX - 5)
        c = sqlite3.connect(self.db)
        c.execute("UPDATE client_traffics SET down=? WHERE email='u1_tun'", (xm.TRAFFIC_MAX - 10,))
        c.commit()
        c.close()
        raw, billed = xm.account(conn, "u1", "u1_tun", 10_000)    # x10 of ~9.2 EB
        self.assertEqual(raw, xm.TRAFFIC_MAX - 10)
        self.assertEqual(billed, xm.TRAFFIC_MAX)
        self.assertEqual(self.traffic("u1")[1], xm.TRAFFIC_MAX, "master saturates like the panel's MIN()")
        led = xm.ledger_row(conn, "u1_tun")
        self.assertLessEqual(led["billed_total"], xm.TRAFFIC_MAX)


class TestDaemonEdges(Base):
    def setUp(self):
        super().setUp()
        self._busy = xm.BUSY_TIMEOUT_MS

    def tearDown(self):
        xm.BUSY_TIMEOUT_MS = self._busy
        super().tearDown()

    def add(self, email, **kw):
        return self.quiet(xm.op_add, email, 2, 1.2, assume_yes=True, **kw)

    def test_one_bad_pair_does_not_stop_the_others(self):
        for e in ("a", "b"):
            mp.seed_client(self.db, e, [1])
            self.add(e)
        self.tick()
        c = sqlite3.connect(self.db)
        c.execute("UPDATE client_traffics SET down='garbage' WHERE email='a_tun'")
        c.commit()
        c.close()
        self.panel.add_traffic("b_tun", down=1000)
        with self.assertLogs("xui-mult", "ERROR"):
            self.tick()
        self.assertEqual(self.traffic("b")[1], 1200)

    def test_locked_database_skips_tick_then_bills_once(self):
        mp.seed_client(self.db, "alice", [1])
        self.add("alice")
        self.tick()
        self.panel.add_traffic("alice_tun", down=1000)
        xm.BUSY_TIMEOUT_MS = 300
        holder = sqlite3.connect(self.db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")        # e.g. the panel inside a long write transaction
        try:
            t0 = time.time()
            with self.assertLogs("xui-mult", "WARNING"):
                self.tick()
            self.assertLess(time.time() - t0, 5)
            self.assertTrue(status()["db_busy"])
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.tick()
        self.tick()
        self.assertEqual(self.traffic("alice")[1], 1200)
        self.assertFalse(status()["db_busy"])

    def test_shadow_reset_rebaselines_immediately(self):
        """After xui-mult resets the shadow, traffic bigger than the old counter is still billed in full."""
        mp.seed_client(self.db, "alice", [1])
        self.add("alice")
        self.tick()
        self.panel.add_traffic("alice_tun", down=1000)
        self.tick()
        self.assertEqual(self.traffic("alice")[1], 1200)
        self.panel.handle("POST", "/clients/resetTraffic/alice", None)   # admin renews the master
        self.tick()
        self.assertEqual(self.traffic("alice_tun")[1], 0)
        self.panel.add_traffic("alice_tun", down=50_000)
        self.tick()
        self.assertEqual(self.traffic("alice")[1], 60_000)

    def test_api_outage_billing_continues_then_recovers(self):
        mp.seed_client(self.db, "alice", [1])
        self.add("alice")
        self.tick()
        self.panel.http_error = 503
        self.panel.add_traffic("alice_tun", down=1000)
        c = sqlite3.connect(self.db)
        c.execute("UPDATE clients SET enable=0 WHERE email='alice'")       # master blocked during the outage
        c.commit()
        c.close()
        with self.assertLogs("xui-mult", "ERROR"):
            self.tick()
        self.assertEqual(self.traffic("alice")[1], 1200, "billing never waits for the API")
        self.assertEqual(status()["api_errors"], 1)
        self.assertEqual(self.traffic("alice_tun")[2], 1)
        self.panel.http_error = None
        self.tick()
        self.assertEqual(self.traffic("alice_tun")[2], 0, "retried on the next tick")
        self.assertEqual(status()["api_errors"], 0)

    def test_bulk_skip_is_an_error_not_silence(self):
        mp.seed_client(self.db, "alice", [1])
        self.add("alice")
        self.tick()
        self.panel.skip_enable = {"alice_tun"}
        c = sqlite3.connect(self.db)
        c.execute("UPDATE clients SET enable=0 WHERE email='alice'")
        c.commit()
        c.close()
        with self.assertLogs("xui-mult", "ERROR"):
            self.tick()
        st = status()
        self.assertEqual(st["api_errors"], 1)
        self.assertIn("skipped alice_tun", st["api_error"])
        self.panel.skip_enable = set()
        self.tick()
        self.assertEqual(self.traffic("alice_tun")[2], 0)

    def test_disable_still_mirrored_when_settings_sync_fails(self):
        mp.seed_client(self.db, "alice", [1], total=10 * GB)
        self.add("alice")
        self.tick()
        c = sqlite3.connect(self.db)
        c.execute("UPDATE clients SET enable=0, total_gb=? WHERE email='alice'", (20 * GB,))
        c.commit()
        c.close()
        with mock.patch.object(xm.PanelAPI, "update_client", side_effect=xm.ApiError("boom")), \
                self.assertLogs("xui-mult", "ERROR"):
            self.tick()
        self.assertEqual(self.traffic("alice_tun")[2], 0)
        self.assertIn("boom", status()["api_error"])


class TestLifecycleEdges(Base):
    def add(self, email="alice", **kw):
        return self.quiet(xm.op_add, email, 2, 1.2, assume_yes=True, **kw)

    def test_master_deleted_disables_shadow(self):
        mp.seed_client(self.db, "alice", [1])
        self.add()
        self.tick()
        self.panel.handle("POST", "/clients/del/alice", None)
        with self.assertLogs("xui-mult", "WARNING"):
            self.tick()
        self.assertEqual(self.traffic("alice_tun")[2], 0, "fail closed")
        self.assertIn("alice", status()["missing"])

    def test_quota_used_up_disables_shadow_in_the_same_tick(self):
        mp.seed_client(self.db, "alice", [1], total=10_000)
        self.add()
        self.tick()
        self.panel.add_traffic("alice_tun", down=9000)      # x1.2 = 10800 >= 10000
        self.tick()                                        # no panel disable job in between
        self.assertEqual(self.traffic("alice")[1], 10_800)
        self.assertEqual(self.traffic("alice")[2], 1, "the panel has not acted yet")
        self.assertEqual(self.traffic("alice_tun")[2], 0, "tunnel blocked without waiting for the panel")
        self.panel.handle("POST", "/clients/update/alice", dict(
            self.panel.handle("GET", "/clients/get/alice", None)[1]["client"],
            id=self.q("SELECT uuid FROM clients WHERE email='alice'")[0][0], totalGB=50_000))
        self.tick()
        self.assertEqual(self.traffic("alice_tun")[2], 1, "re-enabled when quota is raised")

    def test_first_use_through_tunnel_starts_master_clock(self):
        mp.seed_client(self.db, "alice", [1], expiry=-30 * DAY)
        self.add()
        self.assertEqual(self.q("SELECT expiry_time FROM clients WHERE email='alice_tun'")[0][0], -30 * DAY)
        self.tick()
        self.panel.add_traffic("alice_tun", down=10)        # the panel starts the shadow's clock
        s_exp = self.q("SELECT expiry_time FROM clients WHERE email='alice_tun'")[0][0]
        self.assertGreater(s_exp, 0)
        self.tick()
        self.assertEqual(self.q("SELECT expiry_time FROM clients WHERE email='alice'")[0][0], s_exp)
        self.assertEqual(self.q("SELECT expiry_time FROM client_traffics WHERE email='alice'")[0][0], s_exp)
        self.assertEqual(self.q("SELECT expiry_time FROM clients WHERE email='alice_tun'")[0][0], s_exp)
        n = len(self.panel.calls)
        self.tick()
        self.assertEqual(len(self.panel.calls), n, "steady state: no API calls")

    def test_admin_switch_to_delayed_start_is_not_overridden(self):
        exp = int(time.time() * 1000) + 5 * DAY
        mp.seed_client(self.db, "alice", [1], expiry=exp)
        self.add()
        self.tick()
        rec = self.panel.handle("GET", "/clients/get/alice", None)[1]["client"]
        self.panel.handle("POST", "/clients/update/alice", dict(rec, id=rec["uuid"], expiryTime=-7 * DAY))
        self.tick()
        self.assertEqual(self.q("SELECT expiry_time FROM clients WHERE email='alice'")[0][0], -7 * DAY)
        self.assertEqual(self.q("SELECT expiry_time FROM clients WHERE email='alice_tun'")[0][0], -7 * DAY)

    def test_shadow_not_in_master_group(self):
        mp.seed_client(self.db, "alice", [1])
        c = sqlite3.connect(self.db)
        c.execute("UPDATE clients SET group_name='reseller-a' WHERE email='alice'")
        c.commit()
        c.close()
        self.add()
        self.assertEqual(self.q("SELECT group_name FROM clients WHERE email='alice_tun'")[0][0], "")

    def test_ledger_upgrade_from_1_0(self):
        c = sqlite3.connect(self.db)
        c.execute(xm.LEDGER_DDL.replace("    mirrored_expiry  INTEGER,\n", ""))
        c.commit()
        c.close()
        conn = xm.db_connect(self.db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tunnel_multiplier_ledger)")}
        self.assertIn("mirrored_expiry", cols)


class TestApiEdges(Base):
    def test_node_sync_token_is_refused_by_setup(self):
        with self.assertRaises(xm.ApiError) as e:
            self.quiet(xm.op_setup, {"api_token": "sync-token"})
        self.assertIn("FULL", str(e.exception))
        self.assertEqual(xm.load_config()["api_token"], mp.TOKEN, "config untouched on failure")

    def test_panel_domain_requires_host_header(self):
        self.panel.domain = "vpn.example.com"
        mp.seed_client(self.db, "alice", [1])
        _, out = self.quiet(xm.op_add, "alice", 2, 1.2, assume_yes=True)   # api_host=auto -> webDomain
        self.assertIn("now pays", out)
        self.assertIn("@vpn.example.com:8443", self.q("SELECT value FROM client_external_links")[0][0])
        cfg = dict(xm.load_config(), api_host="")
        with self.assertRaises(xm.ApiError) as e:
            xm.api_from_config(cfg).ping()
        self.assertIn("--api-host", str(e.exception))

    def test_unauthorised_and_unreachable(self):
        cfg = dict(xm.load_config(), api_token="wrong")
        with self.assertRaisesRegex(xm.ApiError, "401"):
            xm.api_from_config(cfg).ping()
        cfg = dict(xm.load_config(), api_url="http://127.0.0.1:9/secret")
        with self.assertRaisesRegex(xm.ApiError, "cannot reach"):
            xm.api_from_config(cfg).ping()


if __name__ == "__main__":
    unittest.main(verbosity=2)
