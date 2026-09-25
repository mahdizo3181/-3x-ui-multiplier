#!/usr/bin/env python3
"""
xui-mult — per-inbound traffic multipliers for 3X-UI (MHSanaei).

Give an inbound a multiplier (e.g. 1.2) and every client attached to it pays k x its traffic. Each
tick the daemon adds (k - 1) x the traffic used since the last tick to the client's own counters in
x-ui.db, so the panel's own quota and expiry enforcement does the rest. Nothing else is changed.

  * membership : the client_inbounds table, which is authoritative in v3.x (client_traffics.inbound_id
                 is a stale legacy pointer). New clients are picked up on the next tick. A client on
                 several multiplied inbounds pays the highest multiplier. 3X-UI keeps ONE traffic
                 counter per client, so a client that is also on a 1.0x inbound pays k x on all of its
                 traffic; `xui-mult list` shows how many such clients each inbound has.
  * billing    : one BEGIN IMMEDIATE transaction per tick with the panel's own atomic form
                 `up = MIN(up + ?, cap)` + a high-water-mark ledger inside x-ui.db, committed together.
                 Fractions carry over, so extra == floor(raw x (k - 1)) exactly.

Verified against 3X-UI v3.8.5 (tag 7ef22f9). Standard library only. Run `xui-mult` for the menu.
"""

import argparse
import contextlib
import fcntl
import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

VERSION = "2.0.0"
PANEL_VERIFIED = "v3.8.5"
SCALE = 1000                   # fixed-point multiplier: 1200 == 1.200x
TRAFFIC_MAX = (1 << 63) - 1    # same cap as the panel's ClampedAddExpr
BUSY_TIMEOUT_MS = 10000        # same as the panel's DSN (_busy_timeout=10000)
SERVICE = "xui-mult"

CONF_DIR = os.environ.get("XUI_MULT_CONF_DIR", "/etc/xui-mult")
RUN_DIR = os.environ.get("XUI_MULT_RUN_DIR", "/run/xui-mult")
CONF_PATH = os.path.join(CONF_DIR, "config.json")
STATUS_PATH = os.path.join(RUN_DIR, "status.json")
DEFAULT_DB = "/etc/x-ui/x-ui.db"

DEFAULT_CONFIG = {
    "db": DEFAULT_DB,
    "interval": 7,
    "inbounds": {},         # {"<inbound id>": multiplier}
}

log = logging.getLogger(SERVICE)


class XMError(Exception):
    """User-facing error."""


# =================================================================================== terminal UI

class C:
    on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    @classmethod
    def w(cls, code, s):
        return f"\033[{code}m{s}\033[0m" if cls.on else str(s)
    red = classmethod(lambda c, s: c.w("31", s))
    green = classmethod(lambda c, s: c.w("32", s))
    yellow = classmethod(lambda c, s: c.w("33", s))
    cyan = classmethod(lambda c, s: c.w("36", s))
    bold = classmethod(lambda c, s: c.w("1", s))
    dim = classmethod(lambda c, s: c.w("2", s))


def ok(msg): print(C.green("✔ ") + msg)
def bad(msg): print(C.red("✖ ") + msg)
def warn(msg): print(C.yellow("! ") + msg)
def info(msg): print(C.cyan("• ") + msg)


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def ask(prompt, default=None, validate=None):
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            val = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            raise KeyboardInterrupt
        if not val and default is not None:
            val = str(default)
        if validate:
            try:
                return validate(val)
            except (ValueError, XMError) as e:
                bad(str(e) or "invalid value")
                continue
        if val:
            return val


def confirm(prompt, default=True):
    d = "Y/n" if default else "y/N"
    try:
        v = input(f"{prompt} [{d}]: ").strip().lower()
    except EOFError:
        return default
    return default if not v else v in ("y", "yes")


def parse_mult(v):
    k = round(float(v), 3)
    if not 1.0 < k <= 10.0:
        raise ValueError("multiplier must be above 1.0 and at most 10.0 (to go back to 1.0, remove it)")
    return k


def mult_fp(k):
    return round(float(k) * SCALE)


def fmt_k(k):
    return f"x{k:g}"


# =================================================================================== files / config

def atomic_write(path, data, mode=0o600):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_config():
    cfg = dict(DEFAULT_CONFIG, inbounds={})
    if os.path.exists(CONF_PATH):
        with open(CONF_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    try:
        cfg["inbounds"] = {int(k): parse_mult(v) for k, v in (cfg.get("inbounds") or {}).items()}
    except (TypeError, ValueError) as e:
        raise XMError(f"config: inbounds: {e}")
    if float(cfg.get("interval", 7)) < 2:
        raise XMError("config: interval must be >= 2 seconds")
    return cfg


@contextlib.contextmanager
def config_txn():
    """Exclusive, atomic read-modify-write of the config file (safe against two CLIs at once)."""
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(os.path.join(CONF_DIR, ".config.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        cfg = load_config()
        yield cfg
        cfg["inbounds"] = dict(sorted(cfg["inbounds"].items()))
        atomic_write(CONF_PATH, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


# =================================================================================== database

LEDGER_DDL = """CREATE TABLE IF NOT EXISTS xui_mult_ledger (
    email       TEXT PRIMARY KEY,
    last_up     INTEGER NOT NULL,
    last_down   INTEGER NOT NULL,
    rem_up      INTEGER NOT NULL DEFAULT 0,
    rem_down    INTEGER NOT NULL DEFAULT 0,
    raw_total   INTEGER NOT NULL DEFAULT 0,
    extra_total INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL
)"""


def db_connect(path):
    """One connection, autocommit, same busy timeout as the panel. The journal mode is NOT changed:
    it is a persistent property of x-ui.db owned by the panel (WAL by default, DELETE if the admin
    set XUI_DB_JOURNAL_MODE), and every write here is a short BEGIN IMMEDIATE, correct in both."""
    if not os.path.exists(path):
        raise XMError(f"database not found: {path} (is 3X-UI installed? PostgreSQL panels are not supported)")
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("client_traffics", "clients", "client_inbounds", "inbounds"):
        if t not in tables:
            conn.close()
            raise XMError(f"table '{t}' missing in {path} — this does not look like 3X-UI v3.x")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(client_traffics)")}
    if not {"email", "up", "down", "enable", "total"} <= cols:
        conn.close()
        raise XMError("client_traffics schema differs from 3X-UI v3.8.5 — refusing to run")
    conn.execute(LEDGER_DDL)
    return conn


def q1(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def all_inbounds(conn):
    rows = conn.execute("SELECT id, remark, protocol, port, enable FROM inbounds ORDER BY id").fetchall()
    return [dict(zip(("id", "remark", "protocol", "port", "enable"), r)) for r in rows]


def inbound_row(conn, inbound_id):
    return next((ib for ib in all_inbounds(conn) if ib["id"] == inbound_id), None)


def client_multipliers(conn, inbound_mults):
    """{email: (k fixed-point, inbound id)} for every client attached to a multiplied inbound.
    A client on several multiplied inbounds pays the highest multiplier (lowest inbound id on a tie)."""
    if not inbound_mults:
        return {}
    ids = sorted(inbound_mults)
    rows = conn.execute(f"""SELECT c.email, ci.inbound_id FROM clients c
                            JOIN client_inbounds ci ON ci.client_id = c.id
                            WHERE ci.inbound_id IN ({",".join("?" * len(ids))})
                            ORDER BY ci.inbound_id""", ids)
    out = {}
    for email, ib in rows:
        k = mult_fp(inbound_mults[ib])
        if email not in out or k > out[email][0]:
            out[email] = (k, ib)
    return out


def bill(conn, inbound_mults, dry_run=False):
    """One tick: add floor(new traffic x (k - 1)) to each client's own counters and advance the ledger,
    all in ONE BEGIN IMMEDIATE transaction. The panel also writes inside immediate transactions, so the
    two can never interleave, and a crash leaves neither the credit nor the ledger half-written.
    Returns ({inbound id: [clients, raw, extra]}, [emails skipped because their counters are unreadable])."""
    now = int(time.time())
    per_ib, skipped = {}, []
    conn.execute("BEGIN IMMEDIATE")
    try:
        mults = client_multipliers(conn, inbound_mults)
        ledger = {r[0]: r[1:] for r in conn.execute("SELECT email, last_up, last_down, rem_up, rem_down "
                                                    "FROM xui_mult_ledger")}
        # Detached, deleted or no longer multiplied: forget the client, so a later re-attach starts
        # from its counters at that moment instead of billing everything used in between.
        gone = [(e,) for e in ledger if e not in mults]
        rows = conn.execute("SELECT email, up, down FROM client_traffics").fetchall() if mults else []
        first, credits, moves = [], [], []
        for email, up, down in rows:
            m = mults.get(email)
            if m is None:
                continue
            try:
                cu, cd = (min(max(int(v or 0), 0), TRAFFIC_MAX) for v in (up, down))
            except (TypeError, ValueError):
                skipped.append(email)
                continue
            led = ledger.get(email)
            if led is None:
                first.append((email, cu, cd, now))   # first sight: never bill traffic from before
                continue
            last_up, last_down, rem_up, rem_down = led
            # A counter that went DOWN was reset (renewal / manual reset): all of it is new traffic.
            du = cu - last_up if cu >= last_up else cu
            dd = cd - last_down if cd >= last_down else cd
            if du == 0 and dd == 0:
                if (cu, cd) != (last_up, last_down):   # reset to zero: follow it
                    moves.append((cu, cd, rem_up, rem_down, 0, 0, now, email))
                continue
            su, sd = du * (m[0] - SCALE) + rem_up, dd * (m[0] - SCALE) + rem_down
            xu, xd = min(su // SCALE, TRAFFIC_MAX), min(sd // SCALE, TRAFFIC_MAX)
            if xu or xd:
                credits.append((xu, TRAFFIC_MAX, xd, TRAFFIC_MAX, email))
            # The high-water mark includes our own credit, so it is never counted as traffic.
            moves.append((min(cu + xu, TRAFFIC_MAX), min(cd + xd, TRAFFIC_MAX), su % SCALE, sd % SCALE,
                          min(du + dd, TRAFFIC_MAX), min(xu + xd, TRAFFIC_MAX), now, email))
            st = per_ib.setdefault(m[1], [0, 0, 0])
            st[0] += 1
            st[1] += du + dd
            st[2] += xu + xd
        if dry_run or not (credits or first or moves or gone):   # idle tick: write nothing
            conn.execute("ROLLBACK")
            return per_ib, skipped
        conn.executemany("UPDATE client_traffics SET up = MIN(up + ?, ?), down = MIN(down + ?, ?) WHERE email = ?",
                         credits)
        conn.executemany("INSERT INTO xui_mult_ledger(email, last_up, last_down, updated_at) VALUES(?,?,?,?)", first)
        conn.executemany(f"""UPDATE xui_mult_ledger SET last_up=?, last_down=?, rem_up=?, rem_down=?,
                                 raw_total = MIN(raw_total + ?, {TRAFFIC_MAX}),
                                 extra_total = MIN(extra_total + ?, {TRAFFIC_MAX}), updated_at=?
                             WHERE email=?""", moves)
        conn.executemany("DELETE FROM xui_mult_ledger WHERE email = ?", gone)
        conn.execute("COMMIT")
        return per_ib, skipped
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def prune_ledger(conn, inbound_mults):
    """Forget clients that no longer pay a multiplier (run by `remove`, so it holds while the daemon is down)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        keep = client_multipliers(conn, inbound_mults)
        gone = [(e,) for (e,) in conn.execute("SELECT email FROM xui_mult_ledger") if e not in keep]
        conn.executemany("DELETE FROM xui_mult_ledger WHERE email = ?", gone)
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


# =================================================================================== daemon

_stop = False
_reload = False


def _on_term(*_):
    global _stop
    _stop = True


def _on_hup(*_):
    global _reload
    _reload = True


def sd_notify(msg):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with contextlib.suppress(OSError), socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.connect(addr)
        s.sendall(msg.encode())


def db_identity(path):
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def run_daemon(args):
    global _reload
    os.makedirs(RUN_DIR, exist_ok=True)
    lock = open(os.path.join(RUN_DIR, "daemon.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise XMError("another xui-mult daemon is already running")
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    signal.signal(signal.SIGHUP, _on_hup)

    cfg, cfg_mtime = load_config(), None
    conn, conn_id = None, None
    started, totals, skipped_seen = time.time(), [0, 0], set()
    log.info("xui-mult %s started (panel verified: %s)", VERSION, PANEL_VERIFIED)
    sd_notify("READY=1")

    while not _stop:
        t0 = time.time()
        # --- config hot-reload (CLI edits, SIGHUP)
        try:
            mt = os.path.getmtime(CONF_PATH) if os.path.exists(CONF_PATH) else None
            if _reload or mt != cfg_mtime:
                cfg, cfg_mtime, _reload = load_config(), mt, False
                log.info("config loaded: %s", ", ".join(f"inbound #{i} {fmt_k(k)}" for i, k in cfg["inbounds"].items())
                         or "no multiplied inbounds")
        except (XMError, ValueError, OSError) as e:
            log.error("config error, keeping previous config: %s", e)

        # --- DB (re)connect: survives panel restarts and DB restores (file replaced -> new inode)
        db_err, busy, err, skipped = "", False, "", []
        try:
            ident = db_identity(cfg["db"])
            if conn is None or ident != conn_id:
                if conn is not None:
                    log.warning("database file was replaced — reconnecting")
                    conn.close()
                conn, conn_id = db_connect(cfg["db"]), ident
        except (XMError, OSError, sqlite3.Error) as e:
            db_err, conn = str(e), None
            log.error("database: %s", e)

        if conn is not None:   # also with no multiplied inbound: bill() forgets clients that left
            try:
                per_ib, skipped = bill(conn, cfg["inbounds"], args.dry_run)
                if per_ib:
                    names = dict(conn.execute("SELECT id, remark FROM inbounds").fetchall())
                    for ib, (n, raw, extra) in sorted(per_ib.items()):
                        totals[0] += raw
                        totals[1] += extra
                        log.info("inbound #%s %s %s: %d client(s) used %s -> %s%s extra", ib, names.get(ib) or "",
                                 fmt_k(cfg["inbounds"][ib]), n, human(raw), "would bill " if args.dry_run else "+",
                                 human(extra))
                if set(skipped) - skipped_seen:
                    log.warning("not billed, unreadable traffic counters: %s", ", ".join(sorted(skipped)))
                skipped_seen = set(skipped)
            except sqlite3.OperationalError as e:
                err, busy = str(e), "locked" in str(e) or "busy" in str(e)
                log.warning("%s (nothing lost, billed in full next tick)", e)
            except sqlite3.DatabaseError as e:
                err = str(e)
                if type(e) is sqlite3.DatabaseError:   # corrupt or replaced file: reconnect next tick
                    db_err = err
                    conn.close()
                    conn = None
                log.error("database: %s", e)
            except Exception as e:  # noqa: BLE001 — the daemon must survive anything and retry
                err = repr(e)
                log.exception("tick failed (nothing written, retried next tick)")

        status = {"pid": os.getpid(), "version": VERSION, "started": int(started), "last_tick": int(time.time()),
                  "tick_ms": int((time.time() - t0) * 1000), "inbounds": len(cfg["inbounds"]),
                  "db_error": db_err, "db_busy": busy, "error": err, "skipped": sorted(skipped),
                  "raw_since_start": totals[0], "extra_since_start": totals[1], "dry_run": args.dry_run}
        with contextlib.suppress(OSError):
            atomic_write(STATUS_PATH, json.dumps(status), 0o644)
        sd_notify("WATCHDOG=1\nSTATUS=" + (f"error: {err}" if err else f"{len(cfg['inbounds'])} inbound(s) ok"))
        if args.once:
            break
        end = time.time() + float(cfg.get("interval", 7))
        while not _stop and not _reload and time.time() < end:
            time.sleep(0.25)
    sd_notify("STOPPING=1")
    if conn:
        conn.close()
    lock.close()
    log.info("stopped")


# =================================================================================== operations

def sh(cmd, **kw):
    """subprocess.run that tolerates a missing binary (containers, tests)."""
    try:
        return subprocess.run(cmd, **kw)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(cmd, 127, "", "")


def reload_daemon():
    sh(["systemctl", "kill", "-s", "HUP", SERVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def inbound_client_counts(conn):
    """{inbound id: (active clients, all clients)}"""
    rows = conn.execute("""SELECT ci.inbound_id, COUNT(*),
                                  SUM(CASE WHEN c.enable AND COALESCE(t.enable, 1) THEN 1 ELSE 0 END)
                           FROM client_inbounds ci JOIN clients c ON c.id = ci.client_id
                           LEFT JOIN client_traffics t ON t.email = c.email
                           GROUP BY ci.inbound_id""").fetchall()
    return {ib: (active or 0, total) for ib, total, active in rows}


def mixed_clients(conn, inbound_mults, mults):
    """{inbound id: n} clients billed at that inbound's multiplier that are also on a lower-multiplier
    inbound (3X-UI can't split their traffic by inbound, so all of it is multiplied)."""
    out = {}
    if not mults:
        return out
    for email, ib in conn.execute("SELECT c.email, ci.inbound_id FROM clients c "
                                  "JOIN client_inbounds ci ON ci.client_id = c.id"):
        m = mults.get(email)
        if m and mult_fp(inbound_mults.get(ib, 1.0)) < m[0]:
            out.setdefault(m[1], set()).add(email)
    return {ib: len(s) for ib, s in out.items()}


def op_set(inbound_id, mult):
    k = parse_mult(mult)
    conn = db_connect(load_config()["db"])
    ib = inbound_row(conn, inbound_id)
    if not ib:
        raise XMError(f"inbound {inbound_id} not found — see `xui-mult list`")
    with config_txn() as cfg:
        old = cfg["inbounds"].get(inbound_id)
        cfg["inbounds"][inbound_id] = k
    reload_daemon()
    n = inbound_client_counts(conn).get(inbound_id, (0, 0))[1]
    what = f"{fmt_k(old)} -> {fmt_k(k)}" if old else fmt_k(k)
    ok(f"inbound #{inbound_id} {ib['remark'] or ''}: {what}. Its {n} client(s), and any added later, pay "
       f"{fmt_k(k)} on traffic from now on.")


def op_remove(inbound_id):
    with config_txn() as cfg:
        if inbound_id not in cfg["inbounds"]:
            raise XMError(f"inbound {inbound_id} has no multiplier")
        del cfg["inbounds"][inbound_id]
    prune_ledger(db_connect(cfg["db"]), cfg["inbounds"])
    reload_daemon()
    ok(f"inbound #{inbound_id} is back to x1 (traffic already billed stays billed)")


def op_list():
    cfg = load_config()
    conn = db_connect(cfg["db"])
    mults = client_multipliers(conn, cfg["inbounds"])
    counts = inbound_client_counts(conn)
    extra = {}
    for email, e in conn.execute("SELECT email, extra_total FROM xui_mult_ledger"):
        if email in mults:
            extra[mults[email][1]] = extra.get(mults[email][1], 0) + (e or 0)
    print(C.bold(f"{'ID':>4}  {'REMARK':<20} {'PROTOCOL:PORT':<16} {'CLIENTS':>9}  {'MULT':>5}  EXTRA BILLED"))
    for ib in all_inbounds(conn):
        active, total = counts.get(ib["id"], (0, 0))
        k = cfg["inbounds"].get(ib["id"])
        line = (f"{ib['id']:>4}  {(ib['remark'] or '')[:20]:<20} {ib['protocol'] + ':' + str(ib['port']):<16} "
                f"{f'{active}/{total}':>9}  {fmt_k(k) if k else 'x1':>5}  "
                f"{human(extra.get(ib['id'], 0)) if k else '—'}")
        print(C.bold(line) if k else line + ("" if ib["enable"] else C.dim("  (disabled)")))
    print(C.dim("CLIENTS = active/total. EXTRA BILLED = added by xui-mult since the multiplier was set."))
    missing = [i for i in cfg["inbounds"] if not any(ib["id"] == i for ib in all_inbounds(conn))]
    for i in missing:
        warn(f"inbound #{i} has a multiplier but no longer exists — `xui-mult remove {i}`")
    for ib, n in sorted(mixed_clients(conn, cfg["inbounds"], mults).items()):
        warn(f"inbound #{ib}: {n} client(s) are also on a lower-multiplier inbound. 3X-UI keeps one traffic "
             f"counter per client, so ALL their traffic is billed {fmt_k(cfg['inbounds'][ib])}.")


def read_status():
    try:
        with open(STATUS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def service_active():
    r = sh(["systemctl", "is-active", SERVICE], capture_output=True, text=True)
    return (r.stdout or "").strip() == "active"


def op_status():
    problems = 0

    def check(cond, good, badmsg, hint=""):
        nonlocal problems
        if cond:
            ok(good)
        else:
            problems += 1
            bad(badmsg + (C.dim(f"  → {hint}") if hint else ""))

    print(C.bold(f"xui-mult {VERSION}  (verified for 3X-UI {PANEL_VERIFIED})"))
    try:
        cfg = load_config()
        ok(f"config {CONF_PATH}: {len(cfg['inbounds'])} multiplied inbound(s)")
    except (XMError, ValueError, OSError) as e:
        bad(f"config: {e}")
        return 1
    active = service_active()
    check(active, "service running", "service is not running", "systemctl start xui-mult")
    st = read_status()
    if st:
        age = int(time.time()) - st["last_tick"]
        check(age < max(60, 5 * cfg["interval"]), f"last tick {age}s ago ({st['tick_ms']} ms)",
              f"last tick {age}s ago — daemon stuck?", "xui-mult logs")
        check(not st.get("error") or st.get("db_busy"), "last tick billed without errors",
              f"last tick failed: {st.get('error')}", "xui-mult logs")
        if st.get("db_busy"):
            warn("the last tick found the database locked by the panel (billed in full on the next one)")
        check(not st.get("skipped"), "all clients' counters readable",
              f"not billed, unreadable counters: {', '.join(st.get('skipped') or [])}", "fix them in the panel")
        if st.get("extra_since_start"):
            info(f"since the service started: {human(st['raw_since_start'])} used on multiplied inbounds "
                 f"-> +{human(st['extra_since_start'])} extra billed")
    elif active:
        warn("no heartbeat yet")
    try:
        conn = db_connect(cfg["db"])
        jm = q1(conn, "PRAGMA journal_mode")[0]
        ok(f"database {cfg['db']} (journal_mode={jm}, busy_timeout={BUSY_TIMEOUT_MS} ms)")
    except (XMError, sqlite3.Error) as e:
        bad(str(e))
        return 1
    ver = (sh(["/usr/local/x-ui/x-ui", "-v"], capture_output=True, text=True, timeout=5,
              stdin=subprocess.DEVNULL).stdout or "").strip()
    if ver:
        (ok if PANEL_VERIFIED.lstrip("v") in ver else warn)(
            f"panel version: {ver}" + ("" if PANEL_VERIFIED.lstrip("v") in ver else
                                       f"  (not verified — tool was verified on {PANEL_VERIFIED})"))
    counts = inbound_client_counts(conn)
    for i, k in cfg["inbounds"].items():
        ib = inbound_row(conn, i)
        check(ib is not None, f"inbound #{i} {(ib or {}).get('remark') or ''} {fmt_k(k)}: "
              f"{counts.get(i, (0, 0))[1]} client(s)", f"inbound #{i} {fmt_k(k)} no longer exists",
              f"xui-mult remove {i}")
    print()
    (ok("all checks passed") if not problems else warn(f"{problems} problem(s)"))
    return 0 if not problems else 1


def op_logs(follow=False, lines=100):
    cmd = ["journalctl", "-u", SERVICE, "-n", str(lines), "--no-pager"]
    if follow:
        cmd = ["journalctl", "-u", SERVICE, "-f", "-n", str(lines)]
    with contextlib.suppress(KeyboardInterrupt):
        sh(cmd)


# =================================================================================== menu

def header():
    try:
        n = len(load_config()["inbounds"])
    except (XMError, ValueError, OSError):
        n = "?"
    st = read_status()
    tick = f"{int(time.time()) - st['last_tick']}s ago" if st else "—"
    svc = C.green("● running") if service_active() else C.red("● stopped")
    print()
    print(C.bold(" xui-mult ") + C.dim(f"v{VERSION}") + " — inbound traffic multipliers for 3X-UI")
    print(f" Service: {svc}   Multiplied inbounds: {n}   Last tick: {tick}")
    print(" " + "─" * 58)


MENU = [
    ("1", "Set / edit multiplier for an inbound"),
    ("2", "List inbounds & multipliers"),
    ("3", "Remove multiplier from an inbound"),
    ("4", "Service status & logs"),
    ("0", "Exit"),
]


def menu():
    while True:
        header()
        for k, label in MENU:
            print(f"  {C.cyan(k)}) {label}")
        try:
            choice = input("\n Choose: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        try:
            if choice == "0":
                return 0
            elif choice == "1":
                op_list()
                ib = ask("Inbound ID", validate=int)
                current = load_config()["inbounds"].get(ib)
                op_set(ib, ask("Multiplier", current or "1.2", validate=parse_mult))
            elif choice == "2":
                op_list()
            elif choice == "3":
                op_list()
                op_remove(ask("Inbound ID", validate=int))
            elif choice == "4":
                op_status()
                if confirm("\nFollow live billing? (Ctrl+C to go back)", True):
                    op_logs(follow=True, lines=20)
                continue
            else:
                bad("unknown option")
                continue
        except KeyboardInterrupt:
            print()
        except (XMError, sqlite3.Error, OSError) as e:
            bad(str(e))
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            input(C.dim("\n Press Enter to continue…"))


# =================================================================================== CLI

def mult_arg(v):
    try:
        return parse_mult(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def build_parser():
    ap = argparse.ArgumentParser(prog="xui-mult", description="Per-inbound traffic multipliers for 3X-UI "
                                 f"(verified on {PANEL_VERIFIED}). Run without arguments for the menu.")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--version", action="version", version=f"xui-mult {VERSION}")
    sp = ap.add_subparsers(dest="cmd")
    s = sp.add_parser("set", help="set or change an inbound's multiplier, e.g. `set 2 1.2`")
    s.add_argument("inbound_id", type=int)
    s.add_argument("multiplier", type=mult_arg)
    sp.add_parser("list", help="inbounds, multipliers, client counts and extra billed")
    s = sp.add_parser("remove", help="put an inbound back to x1")
    s.add_argument("inbound_id", type=int)
    sp.add_parser("status", help="health check (exit code 1 on problems)")
    s = sp.add_parser("logs", help="service logs with the billing of every tick")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-n", type=int, default=100)
    s = sp.add_parser("run", help="daemon loop (used by systemd)")
    s.add_argument("--dry-run", action="store_true", help="compute billing, write nothing")
    s.add_argument("--once", action="store_true")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.no_color:
        C.on = False
    if args.cmd == "run":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    else:
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
    if args.cmd in (None, "set", "remove", "run") and os.geteuid() != 0:
        bad("run as root (sudo xui-mult …)")
        return 1
    try:
        if args.cmd is None:
            return menu()
        if args.cmd == "run":
            run_daemon(args)
        elif args.cmd == "set":
            op_set(args.inbound_id, args.multiplier)
        elif args.cmd == "list":
            op_list()
        elif args.cmd == "remove":
            op_remove(args.inbound_id)
        elif args.cmd == "status":
            return op_status()
        elif args.cmd == "logs":
            op_logs(args.follow, args.n)
    except KeyboardInterrupt:
        print()
        return 130
    except (XMError, sqlite3.Error, OSError) as e:
        bad(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
