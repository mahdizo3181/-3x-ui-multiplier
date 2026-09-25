"""Stand-in for what xui-mult touches in a 3X-UI v3.8.5 database (same tables and columns), plus the
panel's own writes to those rows: the atomic traffic add, the traffic reset and the depleted-client
disable job. xui-mult never calls the panel API, so no HTTP server is needed."""
import sqlite3
import threading
import time
import uuid

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE inbounds(id INTEGER PRIMARY KEY, remark TEXT, protocol TEXT, port INT, tag TEXT, enable INT DEFAULT 1,
                      up INT DEFAULT 0, down INT DEFAULT 0);
CREATE TABLE clients(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, sub_id TEXT, uuid TEXT,
  total_gb INT DEFAULT 0, expiry_time INT DEFAULT 0, enable INT DEFAULT 1, created_at INT, updated_at INT);
CREATE TABLE client_inbounds(client_id INT, inbound_id INT, flow_override TEXT DEFAULT '', created_at INT,
  PRIMARY KEY(client_id, inbound_id));
CREATE TABLE client_traffics(id INTEGER PRIMARY KEY AUTOINCREMENT, inbound_id INT, enable INT DEFAULT 1,
  email TEXT UNIQUE, up INT DEFAULT 0, down INT DEFAULT 0, expiry_time INT DEFAULT 0, total INT DEFAULT 0,
  reset INT DEFAULT 0, last_online INT DEFAULT 0);
"""


def create_db(path):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.executemany("INSERT INTO inbounds(id,remark,protocol,port,tag) VALUES(?,?,?,?,?)",
                  [(1, "Direct", "vless", 443, "in-443"), (2, "Germany Tunnel", "vless", 8443, "in-8443"),
                   (3, "Tunnel 2", "trojan", 2083, "in-2083")])
    c.commit()
    c.close()


def seed_client(path, email, inbound_ids, total=0, up=0, down=0, enable=True):
    c = sqlite3.connect(path, timeout=10, isolation_level=None)
    now = int(time.time() * 1000)
    c.execute("BEGIN IMMEDIATE")
    cid = c.execute("INSERT INTO clients(email,sub_id,uuid,total_gb,enable,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (email, uuid.uuid4().hex[:16], str(uuid.uuid4()), total, int(enable), now, now)).lastrowid
    for ib in inbound_ids:
        c.execute("INSERT INTO client_inbounds(client_id,inbound_id,created_at) VALUES(?,?,?)", (cid, ib, now))
    # inbound_id here is the panel's stale legacy pointer: always the FIRST inbound, like v3.8.5
    c.execute("INSERT INTO client_traffics(inbound_id,enable,email,up,down,total) VALUES(?,?,?,?,?,?)",
              (inbound_ids[0] if inbound_ids else 0, int(enable), email, up, down, total))
    c.execute("COMMIT")
    c.close()


class Panel:
    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()   # the panel serialises its own writes (submitTrafficWrite)

    def conn(self):
        c = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def _tx(self, *stmts):
        with self.lock:
            c = self.conn()
            try:
                c.execute("BEGIN IMMEDIATE")   # _txlock=immediate
                out = [c.execute(sql, args).fetchall() for sql, args in stmts]
                c.execute("COMMIT")
                return out
            finally:
                c.close()

    def add_traffic(self, email, up=0, down=0):
        """addClientTraffic: atomic add of Xray's per-user delta."""
        self._tx((("UPDATE client_traffics SET up = MIN(up + ?, 9223372036854775807), "
                   "down = MIN(down + ?, 9223372036854775807) WHERE email = ?"), (up, down, email)))

    def reset_traffic(self, email):
        """resetClientTraffic: zero the counters and re-enable."""
        self._tx(("UPDATE client_traffics SET up=0, down=0, enable=1 WHERE email=?", (email,)),
                 ("UPDATE clients SET enable=1 WHERE email=?", (email,)))

    def disable_invalid(self):
        """disableInvalidClients (depletedClientsCondLocal): quota used up or expired -> disabled."""
        now = int(time.time() * 1000)
        rows = self._tx((("SELECT email FROM client_traffics WHERE enable=1 AND ((total>0 AND up+down>=total) OR "
                          "(expiry_time>0 AND expiry_time<=?))"), (now,)))[0]
        for (e,) in rows:
            self._tx(("UPDATE client_traffics SET enable=0 WHERE email=?", (e,)),
                     ("UPDATE clients SET enable=0 WHERE email=?", (e,)))
        return [r[0] for r in rows]

    def attach(self, email, inbound_id):
        self._tx((("INSERT OR IGNORE INTO client_inbounds(client_id,inbound_id) "
                   "SELECT id, ? FROM clients WHERE email=?"), (inbound_id, email)))

    def detach(self, email, inbound_id):
        self._tx(("DELETE FROM client_inbounds WHERE inbound_id=? AND client_id=(SELECT id FROM clients WHERE email=?)",
                  (inbound_id, email)))

    def delete_client(self, email):
        self._tx(("DELETE FROM client_inbounds WHERE client_id=(SELECT id FROM clients WHERE email=?)", (email,)),
                 ("DELETE FROM clients WHERE email=?", (email,)),
                 ("DELETE FROM client_traffics WHERE email=?", (email,)))
