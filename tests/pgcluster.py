"""A throwaway PostgreSQL server for the tests: initdb in a temp dir, listening on 127.0.0.1 only, fsync off.
One cluster per test run; every test gets its own database. Needs the server binaries (initdb, pg_ctl) and a
Python driver (psycopg2 or psycopg); without them `available()` says why not and the PostgreSQL tests skip.

    python3 tests/pgcluster.py     start one, print its DSN, wait for Ctrl+C (handy for poking around)
"""
import atexit
import glob
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import xui_mult as xm  # noqa: E402

_cluster = None


def _bin(name):
    found = shutil.which(name)
    if found:
        return found
    for p in sorted(glob.glob(f"/usr/lib/postgresql/*/bin/{name}"), reverse=True):   # Debian keeps them off PATH
        return p
    return None


def available():
    """(True, '') or (False, reason)."""
    if xm.pg is None:
        return False, "no PostgreSQL Python driver (pip install psycopg2-binary)"
    if not (_bin("initdb") and _bin("pg_ctl")):
        return False, "PostgreSQL server binaries (initdb, pg_ctl) not found"
    if os.geteuid() == 0:
        return False, "initdb refuses to run as root"
    return True, ""


class Cluster:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="xuimult-pg-")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        data = os.path.join(self.root, "data")
        subprocess.run([_bin("initdb"), "-D", data, "-U", "xui", "-A", "trust", "--no-sync", "-E", "UTF8"],
                       check=True, capture_output=True)
        opts = (f"-p {self.port} -c listen_addresses=127.0.0.1 -c unix_socket_directories= -c fsync=off "
                "-c synchronous_commit=off -c full_page_writes=off -c max_connections=60 -c shared_buffers=64MB "
                "-c log_min_messages=fatal -c autovacuum=off")
        subprocess.run([_bin("pg_ctl"), "-D", data, "-o", opts, "-l", os.path.join(self.root, "pg.log"), "-w",
                        "-t", "60", "start"], check=True, capture_output=True)
        self.data, self.n = data, 0
        self._admin = self.connect("postgres")
        atexit.register(self.stop)

    def dsn(self, dbname, form="url"):
        if form == "url":
            return f"postgres://xui:secret@127.0.0.1:{self.port}/{dbname}?sslmode=disable"
        return f"host=127.0.0.1 port={self.port} user=xui password=secret dbname={dbname} sslmode=disable"

    def connect(self, dbname):
        c = xm.pg.connect(self.dsn(dbname))
        c.autocommit = True
        return c

    def new_database(self, template=None):
        """A fresh, empty database (or a copy of `template`); returns its name."""
        self.n += 1
        name = f"t{self.n}"
        cur = self._admin.cursor()
        cur.execute(f'CREATE DATABASE {name}' + (f' TEMPLATE {template}' if template else ""))
        cur.close()
        return name

    def drop_database(self, name):
        cur = self._admin.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        cur.close()

    def stop(self):
        global _cluster
        _cluster = None
        try:
            self._admin.close()
        except Exception:  # noqa: BLE001
            pass
        subprocess.run([_bin("pg_ctl"), "-D", self.data, "-m", "immediate", "-w", "stop"], capture_output=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def restart(self):
        """Stop and start the server (connections drop), as a PostgreSQL upgrade or crash would."""
        self._admin.close()
        subprocess.run([_bin("pg_ctl"), "-D", self.data, "-m", "fast", "-w", "-l", os.path.join(self.root, "pg.log"),
                        "restart"], check=True, capture_output=True)    # -l: else the server keeps our pipe open
        for _ in range(100):
            try:
                self._admin = self.connect("postgres")
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        raise RuntimeError("PostgreSQL did not come back")


def get():
    global _cluster
    if _cluster is None:
        _cluster = Cluster()
    return _cluster


if __name__ == "__main__":
    ok, why = available()
    if not ok:
        raise SystemExit(why)
    c = get()
    print("DSN:", c.dsn("postgres"))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
