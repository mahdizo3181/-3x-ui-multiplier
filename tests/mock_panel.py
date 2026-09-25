"""Minimal stand-in for 3X-UI v3.8.5: same tables, same /panel/api client endpoints and
validation rules that xui-mult depends on, plus a traffic job with the panel's write semantics."""
import json
import sqlite3
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "test-token"
TOKENS = {TOKEN: "admin", "sync-token": "node-sync"}
# v3.8.5 nodeSyncScopeAllow (subset xui-mult could touch): a node-sync token may list inbounds but not
# enable/disable, get, link or attach clients.
NODE_SYNC_ALLOW = {("GET", "/inbounds/list"), ("POST", "/clients/add"), ("POST", "/clients/del/:email"),
                   ("POST", "/clients/update/:email"), ("POST", "/clients/:email/detach"),
                   ("POST", "/clients/resetTraffic/:email")}


def route_pattern(path):
    parts = path.split("/")
    if len(parts) >= 4 and parts[1] == "clients" and parts[2] in ("get", "links", "update", "del", "resetTraffic"):
        return "/".join(parts[:3] + [":email"])
    if len(parts) == 4 and parts[1] == "clients" and parts[3] in ("attach", "detach", "externalLinks"):
        return f"/clients/:email/{parts[3]}"
    return path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE settings(id INTEGER PRIMARY KEY, key TEXT, value TEXT);
CREATE TABLE inbounds(id INTEGER PRIMARY KEY, remark TEXT, protocol TEXT, port INT, tag TEXT, enable INT DEFAULT 1,
                      up INT DEFAULT 0, down INT DEFAULT 0);
CREATE TABLE clients(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, sub_id TEXT, uuid TEXT,
  password TEXT DEFAULT '', auth TEXT DEFAULT '', flow TEXT DEFAULT '', security TEXT DEFAULT '', reverse TEXT DEFAULT '',
  limit_ip INT DEFAULT 0, limit_hwid INT DEFAULT 0, total_gb INT DEFAULT 0, expiry_time INT DEFAULT 0,
  enable INT DEFAULT 1, tg_id INT DEFAULT 0, group_name TEXT DEFAULT '', comment TEXT DEFAULT '', reset INT DEFAULT 0,
  reset_day INT DEFAULT 0, reset_max INT DEFAULT 0, traffic_reset TEXT DEFAULT 'never', traffic_reset_day INT DEFAULT 1,
  created_at INT, updated_at INT);
CREATE TABLE client_inbounds(client_id INT, inbound_id INT, flow_override TEXT DEFAULT '', created_at INT,
  PRIMARY KEY(client_id, inbound_id));
CREATE TABLE client_traffics(id INTEGER PRIMARY KEY AUTOINCREMENT, inbound_id INT, enable INT DEFAULT 1,
  email TEXT UNIQUE, up INT DEFAULT 0, down INT DEFAULT 0, all_time INT DEFAULT 0, expiry_time INT DEFAULT 0,
  total INT DEFAULT 0, reset INT DEFAULT 0, last_online INT DEFAULT 0);
CREATE TABLE client_external_links(id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INT, kind TEXT, value TEXT,
  remark TEXT, enable INT DEFAULT 1, expiry_time INT DEFAULT 0, name_prefix TEXT DEFAULT '', sort_index INT DEFAULT 0);
"""

COLS = {"uuid": "uuid", "password": "password", "auth": "auth", "flow": "flow", "security": "security",
        "limitIp": "limit_ip", "totalGB": "total_gb", "expiryTime": "expiry_time", "enable": "enable",
        "tgId": "tg_id", "subId": "sub_id", "group": "group_name", "comment": "comment", "reset": "reset",
        "resetDay": "reset_day", "resetMax": "reset_max", "trafficReset": "traffic_reset",
        "trafficResetDay": "traffic_reset_day", "email": "email"}


def create_db(path):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.executemany("INSERT INTO settings(key,value) VALUES(?,?)",
                  [("webPort", "2053"), ("webBasePath", "/secret/"), ("webCertFile", ""), ("webDomain", "vpn.example.com")])
    c.executemany("INSERT INTO inbounds(id,remark,protocol,port,tag) VALUES(?,?,?,?,?)",
                  [(1, "direct", "vless", 443, "in-443"), (2, "iran-tunnel", "vless", 8443, "in-8443")])
    c.commit()
    c.close()


def seed_client(path, email, inbound_ids, total=0, expiry=0, limit_ip=0, enable=True, up=0, down=0):
    c = sqlite3.connect(path, isolation_level=None)
    now = int(time.time() * 1000)
    c.execute("BEGIN IMMEDIATE")
    cur = c.execute("INSERT INTO clients(email,sub_id,uuid,total_gb,expiry_time,limit_ip,enable,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (email, uuid.uuid4().hex[:16], str(uuid.uuid4()), total, expiry, limit_ip, int(enable), now, now))
    cid = cur.lastrowid
    for ib in inbound_ids:
        c.execute("INSERT INTO client_inbounds(client_id,inbound_id,created_at) VALUES(?,?,?)", (cid, ib, now))
    c.execute("INSERT INTO client_traffics(inbound_id,enable,email,up,down,total,expiry_time) VALUES(?,?,?,?,?,?,?)",
              (inbound_ids[0], int(enable), email, up, down, total, expiry))
    c.execute("COMMIT")
    c.close()


class Panel:
    def __init__(self, db_path):
        self.db_path = db_path
        self.fail_next_add = False
        self.skip_enable = set()      # emails bulkEnable/bulkDisable report as skipped
        self.domain = None            # webDomain: when set, other Host headers get a bare 403
        self.http_error = None        # e.g. 503 to simulate an outage
        self.calls = []
        self.lock = threading.Lock()   # the panel serialises its own writes (submitTrafficWrite)

    def conn(self):
        c = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        c.execute("PRAGMA busy_timeout=10000")
        return c

    # ---------------------------------------------------------------- traffic job (panel semantics)
    def add_traffic(self, email, up=0, down=0):
        with self.lock:
            c = self.conn()
            c.execute("BEGIN IMMEDIATE")   # _txlock=immediate
            c.execute("UPDATE client_traffics SET up = MIN(up + ?, 9223372036854775807), "
                      "down = MIN(down + ?, 9223372036854775807) WHERE email = ?", (up, down, email))
            # adjustTraffics: a negative expiry (start after first use) becomes now + duration
            now = int(time.time() * 1000)
            for t in ("client_traffics", "clients"):
                c.execute(f"UPDATE {t} SET expiry_time = ? - expiry_time WHERE email = ? AND expiry_time < 0",
                          (now, email))
            c.execute("COMMIT")
            c.close()

    def disable_invalid(self):
        """Like disableInvalidClients: depleted or expired -> enable=false in both tables."""
        with self.lock:
            c = self.conn()
            now = int(time.time() * 1000)
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT email FROM client_traffics WHERE enable=1 AND ((total>0 AND up+down>=total) OR "
                             "(expiry_time>0 AND expiry_time<=?))", (now,)).fetchall()
            for (e,) in rows:
                c.execute("UPDATE client_traffics SET enable=0 WHERE email=?", (e,))
                c.execute("UPDATE clients SET enable=0 WHERE email=?", (e,))
            c.execute("COMMIT")
            c.close()
            return [r[0] for r in rows]

    # ---------------------------------------------------------------- API handlers
    def record(self, c, email):
        r = c.execute("SELECT * FROM clients WHERE email=?", (email,)).fetchone()
        if not r:
            return None
        names = [d[0] for d in c.execute("SELECT * FROM clients LIMIT 0").description]
        row = dict(zip(names, r))
        rec = {k: row[v] for k, v in COLS.items()}
        rec["enable"] = bool(rec["enable"])
        rec["id"] = row["id"]
        rec["reverse"] = row["reverse"] or ""   # ClientRecord.Reverse is JSON text
        rec["limitHwid"] = row["limit_hwid"]
        return rec

    def handle(self, method, path, body):
        self.calls.append((method, path, body))
        c = self.conn()
        try:
            return self._handle(c, method, path, body)
        finally:
            c.close()

    def _handle(self, c, method, path, body):
        p = urllib.parse.unquote
        path = path.split("?")[0]
        parts = path.split("/")
        now = int(time.time() * 1000)
        if path == "/inbounds/list":
            return True, [dict(id=r[0]) for r in c.execute("SELECT id FROM inbounds")]
        if path == "/clients/list/paged":
            return True, {"clients": [], "total": c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]}
        if parts[1] == "clients" and parts[2] == "get":
            email = p(parts[3])
            rec = self.record(c, email)
            if not rec:
                return False, "record not found"
            ids = [r[0] for r in c.execute("SELECT inbound_id FROM client_inbounds WHERE client_id=?", (rec["id"],))]
            links = [dict(kind=r[0], value=r[1], remark=r[2], enable=bool(r[3]), expiryTime=r[4], namePrefix=r[5])
                     for r in c.execute("SELECT kind,value,remark,enable,expiry_time,name_prefix FROM "
                                        "client_external_links WHERE client_id=? ORDER BY sort_index", (rec["id"],))]
            return True, {"client": rec, "inboundIds": ids, "externalLinks": links}
        if parts[1] == "clients" and parts[2] == "links":
            email = p(parts[3])
            rec = self.record(c, email)
            if not rec:
                return False, "not found"
            host = self.last_host.split(":")[0]
            ids = [r[0] for r in c.execute("SELECT i.port FROM client_inbounds ci JOIN inbounds i ON i.id=ci.inbound_id"
                                           " WHERE ci.client_id=?", (rec["id"],))]
            return True, [f"vless://{rec['uuid']}@{host}:{port}#{email}" for port in ids]
        if path == "/clients/add":
            if self.fail_next_add:
                self.fail_next_add = False
                return False, "simulated failure"
            cl, ibs = body["client"], body["inboundIds"]
            if c.execute("SELECT 1 FROM clients WHERE email=?", (cl["email"],)).fetchone():
                return False, "email already in use: " + cl["email"]
            if cl.get("subId") and c.execute("SELECT 1 FROM clients WHERE sub_id=? AND email<>?",
                                             (cl["subId"], cl["email"])).fetchone():
                return False, "subId already in use: " + cl["subId"]
            with self.lock:
                c.execute("BEGIN IMMEDIATE")
                cid = c.execute("INSERT INTO clients(email,sub_id,uuid,password,total_gb,expiry_time,limit_ip,enable,"
                                "comment,group_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                (cl["email"], cl.get("subId") or uuid.uuid4().hex, cl.get("id") or str(uuid.uuid4()),
                                 cl.get("password", ""), cl.get("totalGB", 0), cl.get("expiryTime", 0),
                                 cl.get("limitIp", 0), int(cl.get("enable", True)), cl.get("comment", ""),
                                 cl.get("group", ""), now, now)).lastrowid
                for ib in ibs:
                    c.execute("INSERT INTO client_inbounds(client_id,inbound_id,created_at) VALUES(?,?,?)", (cid, ib, now))
                c.execute("INSERT INTO client_traffics(inbound_id,enable,email,total,expiry_time) VALUES(?,?,?,?,?)",
                          (ibs[0], int(cl.get("enable", True)), cl["email"], cl.get("totalGB", 0), cl.get("expiryTime", 0)))
                c.execute("COMMIT")
            return True, None
        if parts[1] == "clients" and parts[2] == "update":
            email = p(parts[3])
            rec = self.record(c, email)
            if not rec:
                return False, "not found"
            if isinstance(body.get("allowedIPs"), str):
                return False, "json: cannot unmarshal string into Go struct field .allowedIPs of type []string"
            with self.lock:
                c.execute("BEGIN IMMEDIATE")
                sets = {v: body[k] for k, v in COLS.items() if k in body and k not in ("uuid", "email")}
                sets["uuid"] = body.get("id")
                c.execute("UPDATE clients SET " + ",".join(f"{k}=?" for k in sets) + " WHERE email=?",
                          [int(v) if isinstance(v, bool) else v for v in sets.values()] + [email])
                c.execute("UPDATE client_traffics SET total=?, expiry_time=?, enable=? WHERE email=?",
                          (body["totalGB"], body["expiryTime"], int(body["enable"]), email))
                c.execute("COMMIT")
            return True, None
        if parts[1] == "clients" and parts[2] == "del":
            email = p(parts[3])
            rec = self.record(c, email)
            if not rec:
                return False, "not found"
            c.execute("DELETE FROM client_inbounds WHERE client_id=?", (rec["id"],))
            c.execute("DELETE FROM clients WHERE email=?", (email,))
            c.execute("DELETE FROM client_traffics WHERE email=?", (email,))
            return True, None
        if parts[1] == "clients" and len(parts) == 4 and parts[3] in ("attach", "detach", "externalLinks"):
            email = p(parts[2])
            rec = self.record(c, email)
            if not rec:
                return False, "not found"
            if parts[3] == "attach":
                for ib in body["inboundIds"]:
                    c.execute("INSERT OR IGNORE INTO client_inbounds(client_id,inbound_id,created_at) VALUES(?,?,?)",
                              (rec["id"], ib, now))
            elif parts[3] == "detach":
                for ib in body["inboundIds"]:
                    c.execute("DELETE FROM client_inbounds WHERE client_id=? AND inbound_id=?", (rec["id"], ib))
            else:
                c.execute("DELETE FROM client_external_links WHERE client_id=?", (rec["id"],))
                for i, l in enumerate(body["externalLinks"]):
                    c.execute("INSERT INTO client_external_links(client_id,kind,value,remark,enable,sort_index) "
                              "VALUES(?,?,?,?,?,?)", (rec["id"], l["kind"], l["value"], l["remark"],
                                                     int(l.get("enable", True)), i))
            return True, None
        if path in ("/clients/bulkEnable", "/clients/bulkDisable"):
            # BulkSetEnableResult: success=true even when emails are skipped
            en = int(path.endswith("Enable"))
            changed, skipped = 0, []
            for e in body["emails"]:
                if e in self.skip_enable:
                    skipped.append({"email": e, "reason": "simulated inbound failure"})
                elif not self.record(c, e):
                    skipped.append({"email": e, "reason": "client not found"})
                else:
                    c.execute("UPDATE clients SET enable=? WHERE email=?", (en, e))
                    c.execute("UPDATE client_traffics SET enable=? WHERE email=?", (en, e))
                    changed += 1
            return True, {"changed": changed, "skipped": skipped}
        if parts[1] == "clients" and parts[2] == "resetTraffic":
            email = p(parts[3])
            with self.lock:
                c.execute("BEGIN IMMEDIATE")
                c.execute("UPDATE client_traffics SET up=0, down=0, enable=1 WHERE email=?", (email,))
                c.execute("UPDATE clients SET enable=1 WHERE email=?", (email,))
                c.execute("COMMIT")
            return True, None
        return False, f"unknown route {method} {path}"

    # ---------------------------------------------------------------- HTTP
    def serve(self):
        panel = self

        class H(BaseHTTPRequestHandler):
            def _bare(self, code, payload=None):
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _do(self, method):
                if panel.domain and self.headers.get("Host", "").rsplit(":", 1)[0] != panel.domain:
                    return self._bare(403)                     # DomainValidatorMiddleware
                if panel.http_error:
                    return self._bare(panel.http_error)
                auth = self.headers.get("Authorization", "")
                scope = TOKENS.get(auth[len("Bearer "):]) if auth.startswith("Bearer ") else None
                if not scope:
                    return self._bare(401)
                prefix = "/secret/panel/api"
                if not self.path.startswith(prefix):
                    return self._bare(404)
                rel = self.path[len(prefix):].split("?")[0]
                if scope != "admin" and (method, route_pattern(rel)) not in NODE_SYNC_ALLOW:
                    return self._bare(403, {"success": False, "msg": "this API token is not permitted to access "
                                                                      "this endpoint", "obj": None})
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                panel.last_host = self.headers.get("Host", "")
                okk, obj = panel.handle(method, self.path[len(prefix):], body)
                out = {"success": okk, "msg": "" if okk else obj, "obj": obj if okk else None}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}/secret"
