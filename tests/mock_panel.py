"""Stand-in for what xui-mult touches in a 3X-UI v3.8.5 database (same tables, columns and indexes, on SQLite
and on PostgreSQL), plus the panel's own writes to those rows: the atomic traffic add, the traffic reset and
the depleted-client disable job. xui-mult never calls the panel API, so no HTTP server is needed.

`make_env("sqlite" | "postgres", tmp)` returns an Env with one interface for both, so a test is written once.
The PostgreSQL schema was dumped from a real v3.8.5 panel running on PostgreSQL (see tests/e2e_real_panel.py)."""
import os
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


# =================================================================================== PostgreSQL

PG_SCHEMA = """
CREATE TABLE inbounds(id BIGSERIAL PRIMARY KEY, user_id BIGINT, up BIGINT, down BIGINT, total BIGINT, remark TEXT,
  enable BOOLEAN, expiry_time BIGINT, listen TEXT, port BIGINT, protocol TEXT, settings TEXT, stream_settings TEXT,
  tag TEXT, sniffing TEXT, node_id BIGINT);
CREATE UNIQUE INDEX uni_inbounds_tag ON inbounds(tag);
CREATE TABLE clients(id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL, sub_id TEXT, uuid TEXT, total_gb BIGINT,
  expiry_time BIGINT, enable BOOLEAN DEFAULT true, created_at BIGINT, updated_at BIGINT);
CREATE UNIQUE INDEX idx_clients_email ON clients(email);
CREATE INDEX idx_clients_email_lower ON clients(lower(email));
CREATE INDEX idx_clients_sub_id ON clients(sub_id);
CREATE TABLE client_inbounds(client_id BIGINT NOT NULL, inbound_id BIGINT NOT NULL, flow_override TEXT,
  created_at BIGINT, PRIMARY KEY(client_id, inbound_id));
CREATE INDEX idx_client_inbounds_inbound_id ON client_inbounds(inbound_id);
CREATE INDEX idx_client_inbounds_client_id ON client_inbounds(client_id);
CREATE TABLE client_traffics(id BIGSERIAL PRIMARY KEY, inbound_id BIGINT, enable BOOLEAN, email TEXT, up BIGINT,
  down BIGINT, expiry_time BIGINT, total BIGINT, reset BIGINT DEFAULT 0, reset_day BIGINT DEFAULT 0,
  reset_max BIGINT DEFAULT 0, reset_count BIGINT DEFAULT 0, last_online BIGINT DEFAULT 0,
  last_sub_fetch BIGINT DEFAULT 0);
CREATE UNIQUE INDEX uni_client_traffics_email ON client_traffics(email);
CREATE INDEX idx_client_traffics_renew ON client_traffics(expiry_time, reset);
CREATE INDEX idx_client_traffics_inbound ON client_traffics(inbound_id);
INSERT INTO inbounds(id, remark, protocol, port, tag, enable, up, down) VALUES
  (1, 'Direct', 'vless', 443, 'in-443', true, 0, 0), (2, 'Germany Tunnel', 'vless', 8443, 'in-8443', true, 0, 0),
  (3, 'Tunnel 2', 'trojan', 2083, 'in-2083', true, 0, 0);
SELECT setval('inbounds_id_seq', 3);
"""


def pg_template(cluster):
    """A database holding the schema, used as the template of every test's own database."""
    name = cluster.new_database()
    c = cluster.connect(name)
    c.cursor().execute(PG_SCHEMA)
    c.close()
    return name


class PgPanel:
    """The panel's writes on PostgreSQL: each one a transaction of atomic statements (LEAST(up + ?, cap)), rows
    touched in whatever order the panel's slices come in."""
    CAP = 9_000_000_000_000_000_000

    def __init__(self, env):
        self.env = env

    def _tx(self, *stmts, pause=0.0):
        c = self.env.raw(autocommit=False)
        try:
            cur = c.cursor()
            out = []
            for sql, args in stmts:
                cur.execute(sql.replace("?", "%s"), args)
                out.append(cur.fetchall() if cur.description else [])
                if pause:
                    time.sleep(pause)
            c.commit()
            return out
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def add_traffic(self, email, up=0, down=0, pause=0.0):
        self._tx(("UPDATE client_traffics SET up = LEAST(up + ?, ?), down = LEAST(down + ?, ?) WHERE email = ?",
                  (up, self.CAP, down, self.CAP, email)), pause=pause)

    def add_traffic_batch(self, deltas, shuffle=False, pause=0.0):
        """addClientTraffic: one transaction, one atomic UPDATE per client with traffic."""
        import random
        deltas = list(deltas)
        if shuffle:
            random.shuffle(deltas)
        self._tx(*[("UPDATE client_traffics SET up = LEAST(up + ?, ?), down = LEAST(down + ?, ?) WHERE email = ?",
                    (u, self.CAP, d, self.CAP, e)) for e, u, d in deltas], pause=pause)

    def reset_traffic(self, email):
        self._tx(("UPDATE client_traffics SET up=0, down=0, enable=true WHERE email=?", (email,)),
                 ("UPDATE clients SET enable=true WHERE email=?", (email,)))

    def disable_invalid(self):
        now = int(time.time() * 1000)
        rows = self._tx((("SELECT email FROM client_traffics WHERE enable AND ((total>0 AND up+down>=total) OR "
                          "(expiry_time>0 AND expiry_time<=?))"), (now,)))[0]
        for (e,) in rows:
            self._tx(("UPDATE client_traffics SET enable=false WHERE email=?", (e,)),
                     ("UPDATE clients SET enable=false WHERE email=?", (e,)))
        return [r[0] for r in rows]

    def attach(self, email, inbound_id):
        self._tx((("INSERT INTO client_inbounds(client_id,inbound_id) SELECT id, ? FROM clients WHERE email=? "
                   "ON CONFLICT DO NOTHING"), (inbound_id, email)))

    def detach(self, email, inbound_id):
        self._tx(("DELETE FROM client_inbounds WHERE inbound_id=? AND client_id=(SELECT id FROM clients WHERE email=?)",
                  (inbound_id, email)))

    def delete_client(self, email):
        self._tx(("DELETE FROM client_inbounds WHERE client_id=(SELECT id FROM clients WHERE email=?)", (email,)),
                 ("DELETE FROM clients WHERE email=?", (email,)),
                 ("DELETE FROM client_traffics WHERE email=?", (email,)))


# =================================================================================== one interface for both

class Env:
    """A panel database plus the panel's own writes, behind one interface (SQL with ? placeholders)."""
    kind = ""
    ref = ""        # what goes in xui-mult's config as "db"
    panel = None

    def used(self, email):
        r = self.q("SELECT up, down FROM client_traffics WHERE email=?", (email,))
        return tuple(r[0]) if r else None


class SqliteEnv(Env):
    kind = "sqlite"

    def __init__(self, tmp):
        self.ref = self.path = os.path.join(tmp, "x-ui.db")
        create_db(self.path)
        self.panel = Panel(self.path)

    def q(self, sql, args=()):
        c = sqlite3.connect(self.path)
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.commit()
            c.close()

    def raw(self, autocommit=True):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def seed(self, email, inbound_ids, total=0, up=0, down=0, enable=True):
        seed_client(self.path, email, inbound_ids, total, up, down, enable)

    def seed_many(self, clients):
        """clients: [(email, [inbound ids], up, down)] — one transaction."""
        c = self.raw()
        now = int(time.time() * 1000)
        c.execute("BEGIN IMMEDIATE")
        for email, ids, up, down in clients:
            cid = c.execute("INSERT INTO clients(email,sub_id,uuid,enable,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                            (email, email, email, now, now)).lastrowid
            c.executemany("INSERT INTO client_inbounds(client_id,inbound_id,created_at) VALUES(?,?,?)",
                          [(cid, i, now) for i in ids])
            c.execute("INSERT INTO client_traffics(inbound_id,enable,email,up,down) VALUES(?,1,?,?,?)",
                      (ids[0], email, up, down))
        c.execute("COMMIT")
        c.close()

    def add_traffic_batch(self, deltas):
        for e, u, d in deltas:
            self.panel.add_traffic(e, up=u, down=d)

    def close(self):
        pass


class PgEnv(Env):
    kind = "postgres"

    def __init__(self, cluster, template):
        self.cluster, self.name = cluster, cluster.new_database(template)
        self.ref = cluster.dsn(self.name)
        self.panel = PgPanel(self)
        self._c = None

    def raw(self, autocommit=True):
        c = self.cluster.connect(self.name)
        c.autocommit = autocommit
        return c

    def q(self, sql, args=()):
        if self._c is None:
            self._c = self.raw()
        cur = self._c.cursor()
        cur.execute(sql.replace("?", "%s"), args)
        out = cur.fetchall() if cur.description else []
        cur.close()
        return out

    def seed(self, email, inbound_ids, total=0, up=0, down=0, enable=True):
        self.seed_many([(email, inbound_ids, up, down)], total=total, enable=enable)

    def seed_many(self, clients, total=0, enable=True):
        now = int(time.time() * 1000)
        c = self.raw(autocommit=False)
        cur = c.cursor()
        emails = [x[0] for x in clients]
        cur.execute("INSERT INTO clients(email, sub_id, uuid, total_gb, enable, created_at, updated_at) "
                    "SELECT e, e, e, %s, %s, %s, %s FROM unnest(%s::text[]) e RETURNING id, email",
                    (total, enable, now, now, emails))
        ids = dict((e, i) for i, e in cur.fetchall())
        links = [(ids[e], ib) for e, inb, _, _ in clients for ib in inb]
        cur.execute("INSERT INTO client_inbounds(client_id, inbound_id, created_at) "
                    "SELECT a, b, %s FROM unnest(%s::bigint[], %s::bigint[]) AS x(a, b)",
                    (now, [a for a, _ in links], [b for _, b in links]))
        cur.execute("INSERT INTO client_traffics(inbound_id, enable, email, up, down, total) "
                    "SELECT a, %s, e, u, d, %s FROM unnest(%s::bigint[], %s::text[], %s::bigint[], %s::bigint[]) "
                    "AS x(a, e, u, d)", (enable, total, [x[1][0] for x in clients], emails,
                                         [x[2] for x in clients], [x[3] for x in clients]))
        c.commit()
        c.close()

    def add_traffic_batch(self, deltas):
        self.panel.add_traffic_batch(deltas)

    def close(self):
        if self._c is not None:
            self._c.close()
        self.cluster.drop_database(self.name)


_template = {}


def make_env(kind, tmp):
    if kind == "sqlite":
        return SqliteEnv(tmp)
    import pgcluster
    cluster = pgcluster.get()
    if cluster not in _template:
        _template[cluster] = pg_template(cluster)
    return PgEnv(cluster, _template[cluster])
