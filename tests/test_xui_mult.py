import contextlib
import io
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import xui_mult as xm  # noqa: E402
import mock_panel as mp  # noqa: E402

GB = 1024 ** 3


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        xm.CONF_DIR = os.path.join(self.tmp, "etc")
        xm.RUN_DIR = os.path.join(self.tmp, "run")
        xm.CONF_PATH = os.path.join(xm.CONF_DIR, "config.json")
        xm.STATUS_PATH = os.path.join(xm.RUN_DIR, "status.json")
        xm.C.on = False
        self.db = os.path.join(self.tmp, "x-ui.db")
        mp.create_db(self.db)
        self.panel = mp.Panel(self.db)
        url = self.panel.serve()
        os.makedirs(xm.CONF_DIR)
        cfg = dict(xm.DEFAULT_CONFIG, db=self.db, api_url=url, api_token=mp.TOKEN, link_host="vpn.example.com",
                   pairs=[])
        with open(xm.CONF_PATH, "w") as f:
            json.dump(cfg, f)

    def tearDown(self):
        self.panel.server.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, sql, args=()):
        c = sqlite3.connect(self.db)
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    def traffic(self, email):
        r = self.q("SELECT up, down, enable, total FROM client_traffics WHERE email=?", (email,))
        return r[0] if r else None

    def tick(self):
        class A:
            dry_run = False
            once = True
        xm._stop = False
        xm.run_daemon(A())

    def quiet(self, fn, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            r = fn(*a, **kw)
        return r, out.getvalue()


class TestAccounting(Base):
    def test_concurrent_billing_is_exact(self):
        """Panel writers (atomic + read-then-save inside IMMEDIATE txns) race the daemon."""
        mp.seed_client(self.db, "u1", [1])
        mp.seed_client(self.db, "u1_tun", [2])
        conn = xm.db_connect(self.db)
        xm.ledger_init(conn, "u1_tun")
        direct, tun, stop = [0], [0], [False]

        def writer():
            c = sqlite3.connect(self.db, timeout=10, isolation_level=None)
            c.execute("PRAGMA busy_timeout=10000")
            while not stop[0]:
                du, dt = random.randint(1, 5000), random.randint(1, 5000)
                c.execute("BEGIN IMMEDIATE")
                if random.random() < 0.5:
                    c.execute("UPDATE client_traffics SET down=down+? WHERE email='u1'", (du,))
                    c.execute("UPDATE client_traffics SET down=down+? WHERE email='u1_tun'", (dt,))
                else:
                    m = c.execute("SELECT down FROM client_traffics WHERE email='u1'").fetchone()[0]
                    s = c.execute("SELECT down FROM client_traffics WHERE email='u1_tun'").fetchone()[0]
                    time.sleep(0.001)
                    c.execute("UPDATE client_traffics SET down=? WHERE email='u1'", (m + du,))
                    c.execute("UPDATE client_traffics SET down=? WHERE email='u1_tun'", (s + dt,))
                c.execute("COMMIT")
                direct[0] += du
                tun[0] += dt
                time.sleep(0.002)

        ths = [threading.Thread(target=writer) for _ in range(2)]
        [t.start() for t in ths]
        ticks, t0 = 0, time.time()
        while time.time() - t0 < 3:
            xm.account(conn, "u1", "u1_tun", 1200)
            ticks += 1
            time.sleep(0.003)
        stop[0] = True
        [t.join() for t in ths]
        xm.account(conn, "u1", "u1_tun", 1200)
        master = self.traffic("u1")[1]
        self.assertGreater(ticks, 100)
        self.assertEqual(master, direct[0] + tun[0] * 1200 // 1000)
        self.assertEqual(self.traffic("u1_tun")[1], tun[0], "shadow row must never be written")
        led = xm.ledger_row(conn, "u1_tun")
        self.assertEqual(led["raw_total"], tun[0])
        self.assertEqual(led["billed_total"], tun[0] * 1200 // 1000)

    def test_reset_detection_and_first_sight_baseline(self):
        mp.seed_client(self.db, "u1", [1])
        mp.seed_client(self.db, "u1_tun", [2], down=5000)
        conn = xm.db_connect(self.db)
        self.assertEqual(xm.account(conn, "u1", "u1_tun", 1200), (0, 0))   # history not billed
        self.assertIsNotNone(xm.ledger_row(conn, "u1_tun"), "baseline must persist (regression)")
        self.panel.add_traffic("u1_tun", down=1000)
        self.assertEqual(xm.account(conn, "u1", "u1_tun", 1200), (1000, 1200))
        conn.execute("UPDATE client_traffics SET down=300 WHERE email='u1_tun'")  # reset + new bytes
        self.assertEqual(xm.account(conn, "u1", "u1_tun", 1200), (300, 360))
        self.assertEqual(self.traffic("u1")[1], 1560)

    def test_remainder_carry(self):
        mp.seed_client(self.db, "u1", [1])
        mp.seed_client(self.db, "u1_tun", [2])
        conn = xm.db_connect(self.db)
        xm.ledger_init(conn, "u1_tun")
        for _ in range(1000):
            self.panel.add_traffic("u1_tun", down=3)       # 3 * 1.234 = 3.702 per tick
            xm.account(conn, "u1", "u1_tun", 1234)
        self.assertEqual(self.traffic("u1")[1], 3000 * 1234 // 1000)


class TestLifecycle(Base):
    def add(self, **kw):
        return self.quiet(xm.op_add, "alice", 2, 1.2, assume_yes=True, **kw)

    def test_add_creates_shadow_and_moves_master(self):
        mp.seed_client(self.db, "alice", [1, 2], total=10 * GB, expiry=int(time.time() * 1000) + 86400000, limit_ip=2)
        self.add()
        m = self.q("SELECT id, uuid, sub_id FROM clients WHERE email='alice'")[0]
        s = self.q("SELECT id, uuid, sub_id, total_gb, limit_ip, expiry_time FROM clients WHERE email='alice_tun'")[0]
        self.assertEqual(m[1], s[1], "same UUID")
        self.assertNotEqual(m[2], s[2], "unique subId (v3.8.5 rule)")
        self.assertEqual(s[3], -(-10 * GB * 1000 // 1200), "fail-safe quota = ceil(Q/k)")
        self.assertEqual(s[4], 2)
        self.assertEqual([r[0] for r in self.q("SELECT inbound_id FROM client_inbounds WHERE client_id=?", (m[0],))], [1])
        self.assertEqual([r[0] for r in self.q("SELECT inbound_id FROM client_inbounds WHERE client_id=?", (s[0],))], [2])
        pairs = xm.load_config()["pairs"]
        self.assertEqual(pairs, [{"master": "alice", "shadow": "alice_tun", "multiplier": 1.2, "inbound_id": 2}])
        links = self.q("SELECT value, remark FROM client_external_links WHERE client_id=?", (m[0],))
        self.assertEqual(len(links), 1)
        self.assertIn("@vpn.example.com:8443", links[0][0])
        self.assertEqual(links[0][1], "xui-mult:alice_tun")
        conn = xm.db_connect(self.db)
        self.assertEqual(xm.ledger_row(conn, "alice_tun")["last_down"], 0)

    def test_add_rolls_back_on_failure(self):
        mp.seed_client(self.db, "alice", [1, 2])
        self.panel.fail_next_add = True
        with self.assertRaises(xm.XMError):
            self.add()
        mid = self.q("SELECT id FROM clients WHERE email='alice'")[0][0]
        self.assertEqual(sorted(r[0] for r in self.q("SELECT inbound_id FROM client_inbounds WHERE client_id=?",
                                                     (mid,))), [1, 2], "master re-attached")
        self.assertEqual(self.q("SELECT COUNT(*) FROM clients WHERE email='alice_tun'")[0][0], 0)
        self.assertEqual(xm.load_config()["pairs"], [])

    def test_add_refuses_duplicates_and_missing(self):
        mp.seed_client(self.db, "alice", [1])
        with self.assertRaises(xm.XMError):
            self.quiet(xm.op_add, "nobody", 2, 1.2, assume_yes=True)
        self.add()
        with self.assertRaises(xm.XMError):
            self.add()
        with self.assertRaises(xm.XMError):
            self.quiet(xm.op_add, "alice", 99, 1.2, assume_yes=True)

    def test_daemon_depletion_renewal_and_sync(self):
        mp.seed_client(self.db, "alice", [1], total=10_000)
        self.add()
        self.tick()
        # 5000 direct + 4200 tunnel*1.2 = 10040 >= 10000 -> panel disables master -> daemon disables shadow
        self.panel.add_traffic("alice", down=5000)
        self.panel.add_traffic("alice_tun", down=4200)
        self.tick()
        self.assertEqual(self.traffic("alice")[1], 5000 + 5040)
        self.assertEqual(self.panel.disable_invalid(), ["alice"])
        self.tick()
        self.assertEqual(self.traffic("alice_tun")[2], 0, "shadow disabled with master")
        # admin renews master (reset traffic + enable) -> shadow reset and re-enabled
        self.panel.handle("POST", "/clients/resetTraffic/alice", None)
        self.tick()
        up, down, en, _ = self.traffic("alice_tun")
        self.assertEqual((up, down, en), (0, 0, 1))
        self.assertEqual(self.traffic("alice")[1], 0, "no double billing after renewal")
        # admin raises quota + changes expiry -> mirrored to shadow (fail-safe quota recomputed)
        exp = int(time.time() * 1000) + 5 * 86400000
        self.panel.handle("POST", "/clients/update/alice", dict(
            self.panel.handle("GET", "/clients/get/alice", None)[1]["client"], id=self.q(
                "SELECT uuid FROM clients WHERE email='alice'")[0][0], totalGB=24_000, expiryTime=exp))
        self.tick()
        self.assertEqual(self.q("SELECT total_gb, expiry_time FROM clients WHERE email='alice_tun'")[0], (20_000, exp))
        # traffic continues to bill after all of this
        self.panel.add_traffic("alice_tun", up=1000)
        self.tick()
        self.assertEqual(self.traffic("alice")[0], 1200)
        st = json.load(open(xm.STATUS_PATH))
        self.assertEqual(st["api_errors"], 0)

    def test_failsafe_trip_while_master_renewed(self):
        """Shadow depleted by its own fail-safe while master is enabled -> reset + enable."""
        mp.seed_client(self.db, "alice", [1], total=12_000)
        self.add()
        self.tick()
        self.panel.add_traffic("alice_tun", down=10_000)   # == fail-safe quota
        self.panel.disable_invalid()
        self.tick()
        # master: 12000 billed -> disabled next panel tick; renewal via reset
        self.panel.disable_invalid()
        self.panel.handle("POST", "/clients/resetTraffic/alice", None)
        self.tick()
        self.assertEqual(self.traffic("alice_tun")[2], 1)
        self.assertEqual(self.traffic("alice_tun")[1], 0)

    def test_remove(self):
        mp.seed_client(self.db, "alice", [1, 2])
        self.add()
        self.panel.add_traffic("alice_tun", down=1000)
        self.quiet(xm.op_remove, "alice", delete_shadow=True, reattach=True)
        self.assertEqual(self.traffic("alice")[1], 1200, "final billing done before delete")
        self.assertIsNone(self.traffic("alice_tun"))
        mid = self.q("SELECT id FROM clients WHERE email='alice'")[0][0]
        self.assertEqual(sorted(r[0] for r in self.q("SELECT inbound_id FROM client_inbounds WHERE client_id=?",
                                                     (mid,))), [1, 2])
        self.assertEqual(self.q("SELECT COUNT(*) FROM client_external_links")[0][0], 0)
        self.assertEqual(xm.load_config()["pairs"], [])

    def test_set_mult_and_add_all(self):
        for e in ("a", "b", "c"):
            mp.seed_client(self.db, e, [1])
        self.quiet(xm.op_add_all, 1, 2, 1.3, assume_yes=True)
        self.assertEqual(sorted(p["shadow"] for p in xm.load_config()["pairs"]), ["a_tun", "b_tun", "c_tun"])
        self.quiet(xm.op_add_all, 1, 2, 1.3, assume_yes=True)   # idempotent
        self.assertEqual(len(xm.load_config()["pairs"]), 3)
        self.quiet(xm.op_set_mult, "b", 2)
        self.tick()
        self.panel.add_traffic("b_tun", down=1000)
        self.tick()
        self.assertEqual(self.traffic("b")[1], 2000)

    def test_db_replaced_reconnects(self):
        mp.seed_client(self.db, "alice", [1])
        self.add()
        self.tick()
        shutil.copy(self.db, self.db + ".bak")
        os.replace(self.db + ".bak", self.db)       # e.g. panel "restore backup"
        self.panel.add_traffic("alice_tun", down=500)
        self.tick()
        self.assertEqual(self.traffic("alice")[1], 600)

    def test_cli_read_commands(self):
        mp.seed_client(self.db, "alice", [1], total=GB)
        self.add()
        self.panel.add_traffic("alice_tun", down=1000)
        for argv in (["list"], ["dry-run"], ["inbounds"], ["status"]):
            rc, out = self.quiet(xm.main, argv + ["--no-color"] if False else argv)
            self.assertIn(rc, (0, 1), argv)
            self.assertNotIn("Traceback", out)
        _, out = self.quiet(xm.main, ["dry-run"])
        self.assertIn("would bill", out)
        _, out = self.quiet(xm.main, ["list"])
        self.assertIn("alice_tun", out)

    def test_bad_token_reported(self):
        cfg = xm.load_config()
        cfg["api_token"] = "wrong"
        with open(xm.CONF_PATH, "w") as f:
            json.dump(cfg, f)
        with self.assertRaises(xm.ApiError) as e:
            xm.api_from_config(cfg).ping()
        self.assertIn("401", str(e.exception))

    def test_detect_panel_url(self):
        conn = xm.db_connect(self.db)
        self.assertEqual(xm.detect_panel_url(conn), ("http://127.0.0.1:2053/secret", "vpn.example.com"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
