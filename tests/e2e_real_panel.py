"""End-to-end run of xui-mult against a REAL 3X-UI panel binary (no Xray needed).

    python3 tests/e2e_real_panel.py /path/to/x-ui

Starts the panel on 127.0.0.1 with a throwaway database in a temp dir, drives it through the real
/panel/api, simulates Xray traffic with the panel's own atomic statement, and checks billing, the
lifecycle mirroring, token scopes and the webDomain Host check. Nothing outside the temp dir is touched.
"""
import contextlib
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import xui_mult as xm  # noqa: E402

DAY = 86400000
failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        failures.append(name)


class Panel:
    def __init__(self, binary, root):
        self.bin, self.root = binary, root
        self.env = dict(os.environ, XUI_DB_FOLDER=f"{root}/db", XUI_LOG_FOLDER=f"{root}/log",
                        XUI_BIN_FOLDER=f"{root}/bin")
        for d in ("db", "log", "bin"):
            os.makedirs(f"{root}/{d}", exist_ok=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.db = f"{root}/db/x-ui.db"
        self.url = f"http://127.0.0.1:{self.port}/secret"
        self.proc = None
        self.cli("setting", "-port", str(self.port), "-webBasePath", "/secret/", "-listenIP", "127.0.0.1",
                 "-username", "admin", "-password", "admin-e2e")
        out = self.cli("setting", "-getApiToken")
        self.token = out.strip().splitlines()[-1].split("apiToken:")[-1].strip()

    def cli(self, *args):
        return subprocess.run([self.bin, *args], env=self.env, cwd=self.root, capture_output=True, text=True,
                              timeout=60, check=True).stdout

    def start(self, host=None):
        self.proc = subprocess.Popen([self.bin, "run"], env=self.env, cwd=self.root, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(100):
            with contextlib.suppress(OSError):
                self.api("GET", "/inbounds/list", host=host)
                return
            time.sleep(0.2)
        raise SystemExit("panel did not start")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(20)
            self.proc = None

    def api(self, method, path, payload=None, token=None, host=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url + "/panel/api" + path, data=data, method=method, headers={
            "Authorization": f"Bearer {token or self.token}", "Content-Type": "application/json"})
        if host:
            req.add_header("Host", host)
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
        if not body.get("success"):
            raise RuntimeError(f"{path}: {body.get('msg')}")
        return body.get("obj")

    def xray_traffic(self, email, up=0, down=0):
        """What the panel's traffic job does with Xray stats: atomic add inside BEGIN IMMEDIATE."""
        c = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE client_traffics SET up = MIN(up + ?, 9223372036854775807), "
                  "down = MIN(down + ?, 9223372036854775807) WHERE email = ?", (up, down, email))
        c.execute("COMMIT")
        c.close()

    def first_use(self, email):
        """The panel's adjustTraffics for a delayed-start client: duration -> absolute deadline, written to
        client_traffics, the clients record and the inbound settings JSON."""
        now = int(time.time() * 1000)
        c = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        c.execute("BEGIN IMMEDIATE")
        for t in ("client_traffics", "clients"):
            c.execute(f"UPDATE {t} SET expiry_time = ? - expiry_time WHERE email = ? AND expiry_time < 0", (now, email))
        for ib_id, settings in c.execute("SELECT id, settings FROM inbounds").fetchall():
            s = json.loads(settings)
            for cl in s.get("clients") or []:
                if cl.get("email") == email and (cl.get("expiryTime") or 0) < 0:
                    cl["expiryTime"] = now - cl["expiryTime"]
                    c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (json.dumps(s, indent=2), ib_id))
        c.execute("COMMIT")
        c.close()

    def row(self, email):
        c = sqlite3.connect(self.db, timeout=10)
        try:
            return c.execute("SELECT up, down, enable, total, expiry_time FROM client_traffics WHERE email=?",
                             (email,)).fetchone()
        finally:
            c.close()


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        r = fn(*a, **kw)
    return r, out.getvalue()


def tick():
    class A:
        dry_run, once = False, True
    xm._stop = False
    xm.run_daemon(A())


def main(binary):
    root = tempfile.mkdtemp(prefix="xui-mult-e2e-")
    xm.CONF_DIR, xm.RUN_DIR = f"{root}/etc", f"{root}/run"
    xm.CONF_PATH, xm.STATUS_PATH = f"{root}/etc/config.json", f"{root}/run/status.json"
    xm.C.on = False
    xm.logging.basicConfig(level=xm.logging.WARNING, format="        log: %(message)s")
    p = Panel(binary, root)
    p.start()
    try:
        ver = p.cli("-v").strip()
        print(f"real panel {ver} on {p.url}  (temp dir {root})")
        for port in (31001, 31002):
            p.api("POST", "/inbounds/add", {
                "remark": f"ib{port}", "enable": True, "port": port, "protocol": "vless", "listen": "",
                "settings": json.dumps({"clients": [], "decryption": "none"}),
                "streamSettings": json.dumps({"network": "tcp", "security": "none"}), "sniffing": "{}"})
        exp = int(time.time() * 1000) + 5 * DAY
        p.api("POST", "/clients/add", {"client": {"email": "alice", "id": "", "enable": True, "totalGB": 10 * 1024**3,
                                                  "expiryTime": exp, "limitIp": 2, "subId": "alicesub00000001",
                                                  "group": "resellers", "comment": "vip"}, "inboundIds": [1]})
        p.api("POST", "/clients/add", {"client": {"email": "bob", "id": "", "enable": True, "subId": "bobsub0000000001"},
                                       "inboundIds": [1, 2]})
        p.api("POST", "/clients/add", {"client": {"email": "carol", "id": "", "enable": True, "expiryTime": -30 * DAY,
                                                  "subId": "carolsub00000001", "limitIp": 1}, "inboundIds": [1]})
        sync = p.api("POST", "/setting/apiTokens/create", {"name": "sync", "scope": "node-sync"})["token"]
        os.makedirs(xm.CONF_DIR)
        with open(xm.CONF_PATH, "w") as f:
            json.dump(dict(xm.DEFAULT_CONFIG, db=p.db, link_host="vpn.example.com"), f)

        print("setup / token scope")
        try:
            quiet(xm.op_setup, {"api_url": p.url, "api_token": sync})
            check("node-sync token refused by setup", False, "accepted")
        except xm.ApiError as e:
            check("node-sync token refused by setup", "FULL" in str(e), e)
        quiet(xm.op_setup, {"api_url": p.url, "api_token": p.token})
        check("admin token accepted", xm.load_config()["api_token"] == p.token)

        print("add pairs")
        alice0 = p.api("GET", "/clients/get/alice")["client"]
        quiet(xm.op_add, "alice", 2, 1.2, assume_yes=True)
        quiet(xm.op_add, "bob", 2, 1.2, assume_yes=True)
        at = p.api("GET", "/clients/get/alice_tun")
        check("shadow has master UUID", at["client"]["uuid"] == alice0["uuid"])
        check("shadow has own subId", at["client"]["subId"] != alice0["subId"])
        check("shadow on tunnel inbound only", at["inboundIds"] == [2], at["inboundIds"])
        check("shadow fail-safe quota = ceil(Q/k)", at["client"]["totalGB"] == -(-10 * 1024**3 * 1000 // 1200))
        check("shadow expiry/limitIp mirrored", (at["client"]["expiryTime"], at["client"]["limitIp"]) == (exp, 2))
        check("shadow not in master's group", at["client"]["group"] == "")
        links = p.api("GET", "/clients/get/alice")["externalLinks"]
        check("tunnel link in master subscription", any(x["remark"] == "xui-mult:alice_tun" and ":31002" in x["value"]
                                                        for x in links), links)
        check("bob moved off the tunnel inbound", p.api("GET", "/clients/get/bob")["inboundIds"] == [1])

        print("billing")
        tick()
        p.xray_traffic("alice_tun", up=1000, down=5000)
        p.xray_traffic("alice", down=300)
        tick()
        tick()
        check("master billed x1.2 exactly", p.row("alice")[:2] == (1200, 6300), p.row("alice"))
        check("shadow row never written", p.row("alice_tun")[:2] == (1000, 5000), p.row("alice_tun"))

        print("lifecycle")
        p.api("POST", "/clients/bulkDisable", {"emails": ["alice"]})
        tick()
        check("master disabled -> shadow disabled",
              not p.api("GET", "/clients/get/alice_tun")["client"]["enable"] and p.row("alice_tun")[2] == 0)
        p.api("POST", "/clients/bulkEnable", {"emails": ["alice"]})
        tick()
        check("master enabled -> shadow enabled", p.api("GET", "/clients/get/alice_tun")["client"]["enable"])
        total = p.row("alice")[3]
        p.xray_traffic("alice", down=total - p.row("alice")[1] - 500)
        p.xray_traffic("alice_tun", down=1000)                      # +1200 billed -> over quota
        tick()
        check("quota used up -> shadow disabled in the same tick", p.row("alice_tun")[2] == 0, p.row("alice_tun"))
        p.api("POST", "/clients/resetTraffic/alice")                # admin renews
        tick()
        check("renewal -> shadow counters reset and re-enabled", p.row("alice_tun")[:3] == (0, 0, 1),
              p.row("alice_tun"))
        p.xray_traffic("alice_tun", down=100_000)
        tick()
        check("full billing right after renewal", p.row("alice")[1] == 120_000, p.row("alice"))
        alice1 = p.api("GET", "/clients/get/alice")["client"]
        same = {k: (alice0[k], alice1[k]) for k in ("uuid", "subId", "limitIp", "totalGB", "expiryTime", "group",
                                                    "comment", "tgId") if alice0[k] != alice1[k]}
        check("master record untouched by xui-mult", not same, same)

        print("start after first use through the tunnel")
        carol0 = p.api("GET", "/clients/get/carol")["client"]
        quiet(xm.op_add, "carol", 2, 1.5, assume_yes=True)
        tick()
        p.first_use("carol_tun")
        s_exp = p.row("carol_tun")[4]
        tick()
        carol1 = p.api("GET", "/clients/get/carol")["client"]
        check("master clock started at the tunnel deadline", carol1["expiryTime"] == s_exp > 0,
              (carol1["expiryTime"], s_exp))
        check("master traffic row has the deadline", p.row("carol")[4] == s_exp)
        check("shadow keeps the deadline", p.api("GET", "/clients/get/carol_tun")["client"]["expiryTime"] == s_exp)
        diff = {k: (carol0[k], carol1[k]) for k in ("uuid", "subId", "limitIp", "totalGB", "group", "enable", "tgId",
                                                    "flow", "comment", "reset") if carol0[k] != carol1[k]}
        check("master update round-trip changed nothing else", not diff, diff)
        rec = p.api("GET", "/clients/get/alice")["client"]
        p.api("POST", "/clients/update/alice", dict(xm.record_to_client(rec), expiryTime=-7 * DAY))
        tick()
        tick()
        check("admin switch to delayed start is mirrored, not overridden",
              (p.row("alice")[4], p.row("alice_tun")[4]) == (-7 * DAY, -7 * DAY), (p.row("alice"), p.row("alice_tun")))

        print("panel domain (webDomain) Host check")
        p.stop()
        c = sqlite3.connect(p.db, timeout=10)
        if c.execute("UPDATE settings SET value='panel.example.test' WHERE key='webDomain'").rowcount == 0:
            c.execute("INSERT INTO settings(key, value) VALUES('webDomain', 'panel.example.test')")
        c.commit()
        c.close()
        p.start(host="panel.example.test")
        cfg = xm.load_config()
        try:
            xm.api_from_config(dict(cfg, api_host="")).ping()
            check("without Host the panel refuses (403)", False, "accepted")
        except xm.ApiError as e:
            check("without Host the panel refuses (403) with a hint", "--api-host" in str(e), e)
        xm.api_from_config(cfg).ping()
        check("api_host=auto sends the panel domain", True)
        p.api("POST", "/clients/bulkDisable", {"emails": ["bob"]}, host="panel.example.test")
        tick()
        check("daemon works behind the domain check", p.row("bob_tun")[2] == 0 and
              json.load(open(xm.STATUS_PATH))["api_errors"] == 0)

        print("remove")
        quiet(xm.op_remove, "bob", delete_shadow=True, reattach=True)
        bob = p.api("GET", "/clients/get/bob", host="panel.example.test")
        check("bob re-attached to both inbounds, link removed",
              sorted(bob["inboundIds"]) == [1, 2] and not bob["externalLinks"], bob["inboundIds"])
        check("bob_tun deleted", p.row("bob_tun") is None)
        rc, out = quiet(xm.op_status)
        check("status runs", rc in (0, 1) and "Traceback" not in out)
    finally:
        p.stop()
        shutil.rmtree(root, ignore_errors=True)
    print(f"\n{'ALL E2E CHECKS PASSED' if not failures else f'{len(failures)} E2E CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(os.path.abspath(sys.argv[1])))
