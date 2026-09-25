"""End-to-end run of xui-mult against a REAL 3X-UI panel binary.

    python3 tests/e2e_real_panel.py /path/to/x-ui

Starts the panel on 127.0.0.1 with a throwaway database in a temp dir, creates inbounds and clients
through its API, simulates Xray's traffic reports with the panel's own atomic statement, and checks the
billing. It also runs the panel's own depletion pass (in production it runs every 5 s in the Xray
traffic job) to prove the panel cuts off a client whose multiplied usage reaches the quota. No Xray is
needed. Nothing outside the temp dir is touched.
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

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        failures.append(name)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Panel:
    def __init__(self, binary, root):
        self.bin, self.root = binary, root
        self.env = dict(os.environ, XUI_DB_FOLDER=f"{root}/db", XUI_LOG_FOLDER=f"{root}/log",
                        XUI_BIN_FOLDER=f"{root}/bin")
        for d in ("db", "log", "bin"):
            os.makedirs(f"{root}/{d}", exist_ok=True)
        self.port = free_port()
        self.db = f"{root}/db/x-ui.db"
        self.url = f"http://127.0.0.1:{self.port}/secret"
        self.proc = None
        self.cli("setting", "-port", str(self.port), "-webBasePath", "/secret/", "-listenIP", "127.0.0.1",
                 "-username", "admin", "-password", "admin-e2e")
        self.token = self.cli("setting", "-getApiToken").strip().splitlines()[-1].split("apiToken:")[-1].strip()

    def cli(self, *args):
        return subprocess.run([self.bin, *args], env=self.env, cwd=self.root, capture_output=True, text=True,
                              timeout=60, check=True).stdout

    def start(self):
        self.proc = subprocess.Popen([self.bin, "run"], env=self.env, cwd=self.root, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(100):
            with contextlib.suppress(OSError):
                self.api("GET", "/inbounds/list")
                return
            time.sleep(0.2)
        raise SystemExit("panel did not start")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(20)

    def api(self, method, path, payload=None):
        req = urllib.request.Request(self.url + "/panel/api" + path, method=method,
                                     data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
        if not body.get("success"):
            raise RuntimeError(f"{path}: {body.get('msg')}")
        return body.get("obj")

    def add_inbound(self, remark, port):
        return self.api("POST", "/inbounds/add", {
            "remark": remark, "enable": True, "port": port, "protocol": "vless", "listen": "127.0.0.1",
            "settings": json.dumps({"clients": [], "decryption": "none"}),
            "streamSettings": json.dumps({"network": "tcp", "security": "none"}), "sniffing": "{}"})["id"]

    def add_client(self, email, inbound_ids, total=0):
        self.api("POST", "/clients/add", {"client": {"email": email, "id": "", "enable": True, "totalGB": total,
                                                     "subId": (email + "0" * 16)[:16]},
                                          "inboundIds": inbound_ids})

    def depletion_pass(self):
        """Run the panel's disableInvalidClients now: restarting Xray builds its config, which runs the
        lifecycle pass first. Without an Xray binary the launch itself then fails, as expected here."""
        try:
            self.api("POST", "/server/restartXrayService")
        except RuntimeError as e:
            if "xray-linux" not in str(e):
                raise

    def xray_traffic(self, email, up=0, down=0):
        """What the panel's traffic job does with Xray's stats: atomic add inside BEGIN IMMEDIATE."""
        c = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE client_traffics SET up = MIN(up + ?, 9223372036854775807), "
                  "down = MIN(down + ?, 9223372036854775807) WHERE email = ?", (up, down, email))
        c.execute("COMMIT")
        c.close()

    def row(self, email):
        c = sqlite3.connect(self.db, timeout=10)
        try:
            return c.execute("SELECT up, down, enable FROM client_traffics WHERE email=?", (email,)).fetchone()
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
        print(f"real panel {p.cli('-v').strip()} on {p.url}  (temp dir {root})")
        direct = p.add_inbound("Direct", free_port())
        tun = p.add_inbound("Germany Tunnel", free_port())
        tun2 = p.add_inbound("Tunnel 2", free_port())
        p.add_client("a", [direct])
        p.add_client("b", [tun])
        p.add_client("m", [direct, tun])
        p.add_client("d", [tun, tun2])
        os.makedirs(xm.CONF_DIR)
        with open(xm.CONF_PATH, "w") as f:
            json.dump(dict(xm.DEFAULT_CONFIG, db=p.db), f)

        print("set multipliers")
        quiet(xm.op_set, tun, 1.2)
        quiet(xm.op_set, tun2, 1.5)
        check("config holds the inbound multipliers", xm.load_config()["inbounds"] == {tun: 1.2, tun2: 1.5})
        tick()

        print("billing")
        for e in ("a", "m", "d"):
            p.xray_traffic(e, down=1000)
        p.xray_traffic("b", up=1000, down=5000)
        tick()
        tick()
        check("direct-only client untouched", p.row("a")[:2] == (0, 1000), p.row("a"))
        check("tunnel client pays x1.2 exactly", p.row("b")[:2] == (1200, 6000), p.row("b"))
        check("client on direct + tunnel pays x1.2 on all traffic", p.row("m")[:2] == (0, 1200), p.row("m"))
        check("client on two multiplied inbounds pays the highest", p.row("d")[:2] == (0, 1500), p.row("d"))
        _, out = quiet(xm.op_list)
        check("list flags the direct + tunnel client", f"inbound #{tun}: 1 client(s) are also on" in out, out)
        row = next((line for line in out.splitlines() if "Germany Tunnel" in line), "")
        check("list shows clients and multiplier", "3/3" in row and "[1.20x]" in row, row)

        print("clients added later")
        p.add_client("e", [tun])
        tick()
        p.xray_traffic("e", down=1000)
        tick()
        check("new client picked up automatically", p.row("e")[:2] == (0, 1200), p.row("e"))

        print("renewal and removal")
        p.api("POST", "/clients/resetTraffic/b")
        tick()
        p.xray_traffic("b", down=500)
        tick()
        check("panel reset followed, then exact billing", p.row("b")[:2] == (0, 600), p.row("b"))
        quiet(xm.op_remove, tun2)
        p.xray_traffic("d", down=1000)
        tick()
        check("after remove, the remaining multiplier applies", p.row("d")[:2] == (0, 1500 + 1200), p.row("d"))

        print("the panel enforces the multiplied quota")
        p.add_client("q", [tun], total=10_000)
        tick()
        p.xray_traffic("q", down=9000)
        p.depletion_pass()
        check("9000 B of a 10000 B quota: still enabled", p.row("q")[2] == 1, p.row("q"))
        tick()                                              # +1800 -> 10800 >= 10000
        p.depletion_pass()
        check("panel disabled the client once its x1.2 usage hit the quota", p.row("q")[:3] == (0, 10_800, 0),
              p.row("q"))

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
