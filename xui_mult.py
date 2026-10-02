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
  * billing    : one transaction per tick, a handful of set-based statements whatever the client count:
                 `up = MIN(up + ?, cap)` (the panel's own atomic form) + a high-water-mark ledger in the
                 panel's database, committed together. Fractions carry over, so extra == floor(raw x (k - 1))
                 exactly. A tick with nothing to bill takes no lock and writes nothing.
  * databases  : SQLite and PostgreSQL, detected from the panel's own settings (XUI_DB_TYPE / XUI_DB_DSN).
                 On PostgreSQL the rows being billed are locked with FOR UPDATE SKIP LOCKED, so the daemon
                 never waits on, and can never deadlock with, the panel.

Verified against 3X-UI v3.8.5 (tag 7ef22f9). SQLite needs only the standard library; PostgreSQL needs
python3-psycopg2 (or psycopg 3). Run `xui-mult` for the menu.
"""

import argparse
import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import namedtuple

try:   # PostgreSQL driver: optional, only needed when the panel runs on PostgreSQL
    import psycopg2 as pg
    PG_DRIVER = "psycopg2"
except ImportError:
    try:
        import psycopg as pg
        PG_DRIVER = "psycopg"
    except ImportError:
        pg, PG_DRIVER = None, None
DB_ERRORS = (sqlite3.Error,) + ((pg.Error,) if pg else ())

VERSION = "2.2.0"
PANEL_VERIFIED = "v3.8.5"
SCALE = 1000                   # fixed-point multiplier: 1200 == 1.200x
TRAFFIC_MAX = 9_000_000_000_000_000_000   # the panel's database.TrafficMax: safely below int64, so +1 delta never overflows
BUSY_TIMEOUT_MS = 10000        # same as the panel's DSN (_busy_timeout=10000)
PG_LOCK_TIMEOUT_S = 5          # PostgreSQL: never wait on a lock longer than this
PG_IDLE_TX_TIMEOUT_S = 60      # PostgreSQL: a stalled daemon must not keep the panel's rows locked
PG_ADVISORY_KEY = 0x58554D31   # "XUM1": serialises our own writers (daemon tick vs CLI)
PG_INSTALL_HINT = ("install the driver: apt install python3-psycopg2  (Debian/Ubuntu) · "
                   "dnf install python3-psycopg2  (RHEL/Fedora) · pacman -S python-psycopg2  (Arch)")
SERVICE = "xui-mult"

CONF_DIR = os.environ.get("XUI_MULT_CONF_DIR", "/etc/xui-mult")
RUN_DIR = os.environ.get("XUI_MULT_RUN_DIR", "/run/xui-mult")
CONF_PATH = os.path.join(CONF_DIR, "config.json")
STATUS_PATH = os.path.join(RUN_DIR, "status.json")
DEFAULT_DB = "/etc/x-ui/x-ui.db"
# The env files the panel's systemd unit loads (x-ui.service.{debian,rhel,arch}); XUI_DB_* live there.
PANEL_ENV_FILES = ("/etc/default/x-ui", "/etc/sysconfig/x-ui", "/etc/conf.d/x-ui")

DEFAULT_CONFIG = {
    "db": "auto",           # "auto" (follow the panel), a SQLite file path, or a PostgreSQL DSN

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
    red = classmethod(lambda c, s: c.w("91", s))
    green = classmethod(lambda c, s: c.w("92", s))
    yellow = classmethod(lambda c, s: c.w("93", s))
    cyan = classmethod(lambda c, s: c.w("96", s))
    bold = classmethod(lambda c, s: c.w("1", s))
    dim = classmethod(lambda c, s: c.w("2", s))
    dim_red = classmethod(lambda c, s: c.w("2;31", s))
    title = classmethod(lambda c, s: c.w("1;96", s))


def ok(msg): print(C.green("✔ ") + msg)
def bad(msg): print(C.red("✖ ") + msg)
def warn(msg): print(C.yellow("! ") + msg)
def info(msg): print(C.cyan("• ") + msg)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
LRM = "‎"   # left-to-right mark: keeps a row's layout LTR in terminals that reorder Persian text


def disp_width(s):
    """Terminal columns taken by s. Colours and zero-width characters take none: the ZWNJ in Persian
    text, diacritics, joiners, direction marks. Wide East Asian characters and emoji take two, a flag
    (two regional indicators) takes two, VS16 makes the previous symbol a two-column emoji, and a
    character after a zero-width joiner is drawn inside the same emoji."""
    w = prev = 0
    joined = False
    for ch in ANSI_RE.sub("", s):
        if ch == "️":
            if prev == 1:
                w, prev = w + 1, 2
            continue
        if unicodedata.category(ch) in ("Mn", "Me", "Cf") or "︀" <= ch <= "︎":
            joined = ch == "‍"
            continue
        if joined:
            joined = False
            continue
        prev = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        w += prev
    return w


def is_rtl(s):
    return any(unicodedata.bidirectional(ch) in ("R", "AL") for ch in s)


def plain(s):
    """Printable single-line text (a remark may hold tabs or newlines)."""
    return "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in str(s))


def fit(s, width):
    """Cut text to at most `width` columns, ending with … when something was cut. Colours are kept."""
    if disp_width(s) <= width:
        return s
    out = ""
    for part in re.split(f"({ANSI_RE.pattern})", s):
        if ANSI_RE.fullmatch(part):
            out += part
            continue
        for ch in part:
            if disp_width(out + ch) + 1 > width:
                return out + "…" + ("\033[0m" if ANSI_RE.search(out) else "")
            out += ch
    return out


def pad(s, width, right=False):
    gap = " " * max(width - disp_width(s), 0)
    return gap + s if right else s + gap


def term_width():
    return shutil.get_terminal_size((100, 24)).columns


def wrap(text, width):
    """Word-wrap plain text to `width` columns."""
    lines, cur = [], ""
    for word in text.split(" "):
        cand = f"{cur} {word}" if cur else word
        if cur and disp_width(cand) > width:
            lines.append(cur)
            cur = word
        else:
            cur = cand
    lines.append(cur)
    return [fit(line, width) for line in lines]


SEP = object()   # a ├───┤ divider inside a card


def card_width():
    """Inner width of cards: the terminal, at most 72 columns."""
    return max(min(term_width(), 76) - 4, 30)


def card(title, rows, footer=""):
    """Rounded card with the title in the top border. Rows may carry colours; wrap them to card_width()
    (anything wider is cut)."""
    inner = card_width()
    b = C.dim
    head = f"─ {title} " if title else ""
    out = [b("╭") + (b("─ ") + C.title(title) + " " if title else "") + b("─" * (inner + 2 - disp_width(head)) + "╮")]
    for r in rows:
        if r is SEP:
            out.append(b("├" + "─" * (inner + 2) + "┤"))
        else:
            out.append(b("│ ") + pad(fit(r, inner), inner) + b(" │"))
    tail = f" {footer} ─" if footer else ""
    out.append(b("╰" + "─" * (inner + 2 - disp_width(tail))) + (" " + C.dim(footer) + b(" ─") if footer else "") + b("╯"))
    return "\n".join(out)


def table(headers, rows, right=(), shrink=()):
    """Box-drawn table aligned on display width (emoji, flags, CJK, Persian). Each cell is
    (plain text, style function or None). When the terminal is too narrow, the `shrink` columns give
    way in that order (down to 8 columns each) and their text is cut with …."""
    rows = [[(plain(t), f) for t, f in r] for r in rows]
    widths = [max([disp_width(h)] + [disp_width(r[i][0]) for r in rows]) for i, h in enumerate(headers)]
    over = sum(widths) + 3 * len(widths) + 1 - term_width()
    for i in shrink:
        if over <= 0:
            break
        new = max(widths[i] - over, min(widths[i], 8))
        over, widths[i] = over - (widths[i] - new), new
    b = C.dim

    def line(cells):
        out, rtl = [], False
        for i, (t, f) in enumerate(cells):
            t = fit(t, widths[i])
            text = pad(t, widths[i], right=i in right)
            if f:   # colour the text, not the padding
                text = text.replace(t, f(t), 1)
            if is_rtl(t):
                rtl, text = True, text + LRM
            out.append(text)
        row = b("│ ") + b(" │ ").join(out) + b(" │")
        return LRM + row if rtl else row

    rule = lambda l, m, r: b(l + m.join("─" * (w + 2) for w in widths) + r)  # noqa: E731
    return "\n".join([rule("╭", "┬", "╮"), line([(h, C.bold) for h in headers]), rule("├", "┼", "┤")]
                     + [line(r) for r in rows] + [rule("╰", "┴", "╯")])


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
    """1.2 -> '1.20x', 1.234 -> '1.234x'"""
    s = f"{float(k):.3f}"
    return (s[:-1] if s.endswith("0") else s) + "x"


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
    if not isinstance(cfg.get("db"), str) or not cfg["db"].strip():
        raise XMError('config: "db" must be "auto", a SQLite file path or a PostgreSQL DSN')
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

# --- which database? Follow the panel: XUI_DB_TYPE / XUI_DB_DSN / XUI_DB_FOLDER, exactly as it reads them.

Target = namedtuple("Target", "kind ref source")


def is_dsn(ref):
    r = ref.strip().lower()
    return r.startswith(("postgres://", "postgresql://")) or (not r.startswith("/") and "=" in r)


def mask_dsn(dsn):
    """A DSN for logs and screens: the password never leaves this function."""
    dsn = re.sub(r"(://[^:/@\s]*:)[^@\s]*@", r"\1***@", dsn)
    return re.sub(r"(?i)(password\s*=\s*)('(?:[^'\\]|\\.)*'|[^\s&]+)", r"\1***", dsn)


def dsn_summary(dsn):
    """user@host:port/dbname, or the masked DSN if the driver cannot parse it."""
    try:
        d = pg.extensions.parse_dsn(dsn) if PG_DRIVER == "psycopg2" else __import__(
            "psycopg.conninfo", fromlist=["conninfo_to_dict"]).conninfo_to_dict(dsn)
        return f"{d.get('user', '')}@{d.get('host', 'localhost')}:{d.get('port', 5432)}/{d.get('dbname', '')}"
    except Exception:  # noqa: BLE001 — cosmetic only
        return mask_dsn(dsn)


def read_env_file(path):
    """KEY=VALUE lines of a systemd EnvironmentFile (what the panel's unit loads)."""
    out = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().removeprefix("export ").strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k] = v
    return out


def panel_env():
    """The panel's XUI_DB_* settings: its env file, overridden by our own environment (for a drop-in)."""
    env = {}
    for path in PANEL_ENV_FILES:
        with contextlib.suppress(OSError):
            env.update(read_env_file(path))
    env.update({k: os.environ[k] for k in ("XUI_DB_TYPE", "XUI_DB_DSN", "XUI_DB_FOLDER") if os.environ.get(k)})
    return env


def resolve_target(cfg):
    """Where the panel's data lives right now. Looked up on every tick, so `x-ui migrate-db` is followed
    without a restart. A database pinned in the config (or XUI_MULT_DB) wins over auto-detection."""
    pinned = os.environ.get("XUI_MULT_DB") or str(cfg.get("db") or "auto")
    if pinned not in ("auto", DEFAULT_DB) or os.environ.get("XUI_MULT_DB"):
        src = "XUI_MULT_DB" if os.environ.get("XUI_MULT_DB") else "pinned in config"
        return Target("postgres" if is_dsn(pinned) else "sqlite", pinned, src)
    env = panel_env()
    if env.get("XUI_DB_TYPE", "").strip().lower() in ("postgres", "postgresql", "pg"):
        dsn = env.get("XUI_DB_DSN", "").strip()
        if not dsn:
            raise XMError("the panel is set to PostgreSQL (XUI_DB_TYPE=postgres) but XUI_DB_DSN is empty")
        return Target("postgres", dsn, "panel settings")
    folder = env.get("XUI_DB_FOLDER")
    return Target("sqlite", os.path.join(folder, "x-ui.db") if folder else DEFAULT_DB,
                  "panel settings" if folder else "default path")


# --- one interface over both databases

LEDGER_DDL = """CREATE TABLE IF NOT EXISTS xui_mult_ledger (
    email       TEXT PRIMARY KEY,
    last_up     BIGINT NOT NULL,
    last_down   BIGINT NOT NULL,
    rem_up      BIGINT NOT NULL DEFAULT 0,
    rem_down    BIGINT NOT NULL DEFAULT 0,
    raw_total   BIGINT NOT NULL DEFAULT 0,
    extra_total BIGINT NOT NULL DEFAULT 0,
    updated_at  BIGINT NOT NULL
){opts}"""
REQUIRED_TABLES = ("client_traffics", "clients", "client_inbounds", "inbounds")
BATCH = 5000        # rows per bulk statement (PostgreSQL): keeps one statement a few hundred KB at most


def batches(seq, n=BATCH):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class Database:
    """One connection and the few statements xui-mult needs. SQL is written with `?` placeholders; the
    subclasses differ only in dialect, locking, and how a batch of rows is written."""
    kind = label = ""
    lock_rows = ""          # appended to the SELECT that fetches the counters about to be credited

    def __init__(self, conn, ref):
        self.conn, self.ref = conn, ref
        self.stmts = 0      # statements sent to the server (an executemany counts once)
        self.last = {"clients": 0, "statements": 0}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        with contextlib.suppress(*DB_ERRORS):
            self.conn.close()

    def check_schema(self):
        for t in REQUIRED_TABLES:
            if not self.table_exists(t):
                raise XMError(f"table '{t}' missing in {self.where()} — this does not look like 3X-UI v3.x")
        if not {"email", "up", "down", "enable", "total"} <= self.columns("client_traffics"):
            raise XMError("client_traffics schema differs from 3X-UI v3.8.5 — refusing to run")

    def any_of(self, col, ids):
        raise NotImplementedError

    def counters(self, ids, lock=False):
        """{email: (up, down)} of every client attached to one of the inbounds. With lock=True the rows are
        locked for the transaction (a no-op on SQLite, whose BEGIN IMMEDIATE already holds the database)."""
        cond, args = self.any_of("ci.inbound_id", ids)
        sql = ("SELECT t.email, t.up, t.down FROM client_traffics t WHERE t.email IN "
               "(SELECT c.email FROM clients c JOIN client_inbounds ci ON ci.client_id = c.id "
               f"WHERE {cond}){self.lock_rows if lock else ''}")
        return {e: (u, d) for e, u, d in self.rows(sql, args)}

    def ledger(self):
        return {r[0]: tuple(r[1:]) for r in self.rows(
            "SELECT email, last_up, last_down, rem_up, rem_down, raw_total, extra_total FROM xui_mult_ledger")}

    def apply(self, plan):
        self.credit(plan.credits)
        self.ledger_insert(plan.first)
        self.ledger_update(plan.moves)
        self.ledger_delete(plan.gone)


class SQLiteDB(Database):
    kind, label = "sqlite", "SQLite"

    @classmethod
    def connect(cls, path, ledger=True):
        """One connection, autocommit, same busy timeout as the panel. The journal mode is NOT changed:
        it is a persistent property of x-ui.db owned by the panel (WAL by default, DELETE if the admin
        set XUI_DB_JOURNAL_MODE), and every write here is a short BEGIN IMMEDIATE, correct in both."""
        if not os.path.exists(path):
            raise XMError(f"database not found: {path} (is 3X-UI installed? PostgreSQL panels: see `xui-mult db`)")
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
        db = cls(conn, path)
        try:
            db.check_schema()
            if ledger:
                db.run(LEDGER_DDL.format(opts=""))
            else:
                db.run("PRAGMA query_only = ON")
        except BaseException:
            db.close()
            raise
        return db

    def where(self):
        return self.ref

    def describe(self):
        jm = self.rows("PRAGMA journal_mode")[0][0]
        return f"SQLite {self.ref} · journal {jm} · busy timeout {BUSY_TIMEOUT_MS // 1000} s"

    @property
    def total_changes(self):
        return self.conn.total_changes

    def rows(self, sql, args=()):
        self.stmts += 1
        return self.conn.execute(sql, args).fetchall()

    def run(self, sql, args=()):
        self.stmts += 1
        self.conn.execute(sql, args)

    def many(self, sql, rows):
        if rows:
            self.stmts += 1
            self.conn.executemany(sql, rows)

    def table_exists(self, t):
        return bool(self.rows("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)))

    def columns(self, t):
        return {r[1] for r in self.rows(f"PRAGMA table_info({t})")}

    def any_of(self, col, ids):
        return f"{col} IN ({','.join('?' * len(ids))})", tuple(ids)

    def begin(self):
        self.run("BEGIN IMMEDIATE")      # the panel also writes inside immediate transactions: never interleaved

    def commit(self):
        self.run("COMMIT")

    def rollback(self):
        with contextlib.suppress(sqlite3.Error):
            self.run("ROLLBACK")

    def is_busy(self, e):
        return isinstance(e, sqlite3.OperationalError) and ("locked" in str(e) or "busy" in str(e))

    def needs_reconnect(self, e):
        return type(e) is sqlite3.DatabaseError      # corrupt or replaced file

    def credit(self, rows):
        self.many("UPDATE client_traffics SET up = MIN(up + ?, ?), down = MIN(down + ?, ?) WHERE email = ?",
                  [(u, TRAFFIC_MAX, d, TRAFFIC_MAX, e) for e, u, d in rows])

    def ledger_insert(self, rows):
        self.many("INSERT INTO xui_mult_ledger(email, last_up, last_down, updated_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(email) DO NOTHING", rows)

    def ledger_update(self, rows):
        self.many("UPDATE xui_mult_ledger SET last_up=?, last_down=?, rem_up=?, rem_down=?, raw_total=?, "
                  "extra_total=?, updated_at=? WHERE email=?", [r[1:] + r[:1] for r in rows])

    def ledger_delete(self, emails):
        self.many("DELETE FROM xui_mult_ledger WHERE email = ?", [(e,) for e in emails])


class PostgresDB(Database):
    """PostgreSQL. Every write is one set-based statement over unnest() arrays, so a tick is a fixed handful of
    round trips for 5 clients or 5000. The counters being credited are locked with FOR UPDATE SKIP LOCKED:
    the panel's own atomic `up = up + ?` then simply waits the few milliseconds we hold them, while we never
    wait on a row the panel holds. With no wait on our side there is no lock cycle, so no deadlock, whatever
    order the panel updates its rows in; a skipped client is billed on the next tick."""
    kind, label = "postgres", "PostgreSQL"
    lock_rows = " FOR UPDATE OF t SKIP LOCKED"

    @classmethod
    def connect(cls, dsn, ledger=True):
        if pg is None:
            raise XMError("the panel uses PostgreSQL but no Python driver is installed — " + PG_INSTALL_HINT)
        try:
            conn = pg.connect(dsn, connect_timeout=10, application_name=SERVICE)
        except pg.Error as e:
            raise XMError(f"cannot connect to PostgreSQL ({dsn_summary(dsn)}): {str(e).strip()}")
        db = cls(conn, dsn)
        try:
            conn.autocommit = True
            db.run(f"SET lock_timeout = {PG_LOCK_TIMEOUT_S * 1000}")
            db.run(f"SET idle_in_transaction_session_timeout = {PG_IDLE_TX_TIMEOUT_S * 1000}")
            db.run("SET statement_timeout = 60000")
            db.check_schema()
            if ledger:
                db.ensure_ledger()
            else:
                db.run("SET default_transaction_read_only = on")
        except BaseException:
            db.close()
            raise
        return db

    def where(self):
        return dsn_summary(self.ref)

    def describe(self):
        ver = self.rows("SHOW server_version")[0][0].split()[0]
        return f"PostgreSQL {ver} · {dsn_summary(self.ref)} · driver {PG_DRIVER}"

    def rows(self, sql, args=()):
        self.stmts += 1
        with self.conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), args)
            return cur.fetchall() if cur.description else []

    def run(self, sql, args=()):
        self.rows(sql, args)

    def table_exists(self, t):
        return self.rows("SELECT to_regclass(?) IS NOT NULL", (t,))[0][0]

    def columns(self, t):
        return {r[0] for r in self.rows("SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(?) "
                                        "AND attnum > 0 AND NOT attisdropped", (t,))}

    def ensure_ledger(self):
        if self.table_exists("xui_mult_ledger"):
            return
        self.run("SELECT pg_advisory_lock(?)", (PG_ADVISORY_KEY,))     # two processes creating it at once would collide
        try:
            # fillfactor: room on the page, so the every-tick ledger updates stay cheap in-page (HOT) updates
            self.run(LEDGER_DDL.format(opts=" WITH (fillfactor = 80)"))
        finally:
            self.run("SELECT pg_advisory_unlock(?)", (PG_ADVISORY_KEY,))

    def any_of(self, col, ids):
        return f"{col} = ANY(?::bigint[])", (list(ids),)

    def begin(self):
        self.run("BEGIN")
        self.run("SELECT pg_advisory_xact_lock(?)", (PG_ADVISORY_KEY,))   # the daemon and `xui-mult remove` take turns

    def commit(self):
        self.run("COMMIT")

    def rollback(self):
        with contextlib.suppress(*DB_ERRORS):
            self.run("ROLLBACK")

    def is_busy(self, e):
        # lock_not_available, deadlock_detected, serialization_failure: nothing was written, retry next tick
        return (getattr(e, "pgcode", None) or getattr(e, "sqlstate", None)) in ("55P03", "40P01", "40001")

    def needs_reconnect(self, e):
        return bool(getattr(self.conn, "closed", 0)) or bool(getattr(self.conn, "broken", False))

    def credit(self, rows):
        for b in batches(rows):
            e, u, d = zip(*b)
            self.run("UPDATE client_traffics t SET up = LEAST(t.up + v.u, ?), down = LEAST(t.down + v.d, ?) "
                     "FROM unnest(?::text[], ?::bigint[], ?::bigint[]) AS v(email, u, d) WHERE t.email = v.email",
                     (TRAFFIC_MAX, TRAFFIC_MAX, list(e), list(u), list(d)))

    def ledger_insert(self, rows):
        for b in batches(rows):
            self.run("INSERT INTO xui_mult_ledger(email, last_up, last_down, updated_at) "
                     "SELECT * FROM unnest(?::text[], ?::bigint[], ?::bigint[], ?::bigint[]) "
                     "ON CONFLICT (email) DO NOTHING", tuple(list(c) for c in zip(*b)))

    def ledger_update(self, rows):
        for b in batches(rows):
            self.run("UPDATE xui_mult_ledger l SET last_up = v.lu, last_down = v.ld, rem_up = v.ru, rem_down = v.rd, "
                     "raw_total = v.rt, extra_total = v.et, updated_at = v.ts "
                     "FROM unnest(?::text[], ?::bigint[], ?::bigint[], ?::bigint[], ?::bigint[], ?::bigint[], "
                     "?::bigint[], ?::bigint[]) AS v(email, lu, ld, ru, rd, rt, et, ts) WHERE l.email = v.email",
                     tuple(list(c) for c in zip(*b)))

    def ledger_delete(self, emails):
        for b in batches(emails):
            self.run("DELETE FROM xui_mult_ledger WHERE email = ANY(?::text[])", (list(b),))


def db_connect(ref, ledger=True):
    """Open the database named by `ref`: a SQLite file path or a PostgreSQL DSN (URL or key=value).
    ledger=False opens it read-only and does not create our table (compatibility checks on a live panel)."""
    return (PostgresDB if is_dsn(ref) else SQLiteDB).connect(ref, ledger)


def open_db(cfg=None):
    return db_connect(resolve_target(cfg or load_config()).ref)


# --- queries

def all_inbounds(db):
    rows = db.rows("SELECT id, remark, protocol, port, enable FROM inbounds ORDER BY id")
    return [dict(zip(("id", "remark", "protocol", "port", "enable"), r)) for r in rows]


def inbound_row(db, inbound_id):
    return next((ib for ib in all_inbounds(db) if ib["id"] == inbound_id), None)


def client_multipliers(db, inbound_mults):
    """{email: (k fixed-point, inbound id)} for every client attached to a multiplied inbound.
    A client on several multiplied inbounds pays the highest multiplier (lowest inbound id on a tie)."""
    if not inbound_mults:
        return {}
    cond, args = db.any_of("ci.inbound_id", sorted(inbound_mults))
    out = {}
    for email, ib in db.rows(f"""SELECT c.email, ci.inbound_id FROM clients c
                                 JOIN client_inbounds ci ON ci.client_id = c.id
                                 WHERE {cond} ORDER BY ci.inbound_id""", args):
        k = mult_fp(inbound_mults[ib])
        if email not in out or k > out[email][0]:
            out[email] = (k, ib)
    return out


class Plan:
    """What one tick will write. Rows are plain tuples, in the shape the batch writers take."""

    def __init__(self):
        self.credits = []        # (email, extra up, extra down)
        self.first = []          # (email, up, down, now)                      first sight: baseline only
        self.moves = []          # (email, last_up, last_down, rem_up, rem_down, raw_total, extra_total, now)
        self.gone = []           # emails to forget
        self.per_ib = {}         # {inbound id: [clients billed, raw bytes, extra bytes]}
        self.skipped = []        # emails whose counters are unreadable
        self.tracked = 0         # clients on multiplied inbounds whose counters were read

    @property
    def work(self):
        return bool(self.credits or self.first or self.moves or self.gone)


def compute_plan(mults, counters, ledger, now):
    """Pure function of what was read: no I/O, so it is the same for every database."""
    plan = Plan()
    # Detached, deleted or no longer multiplied: forget the client, so a later re-attach starts from its
    # counters at that moment instead of billing everything used in between.
    plan.gone = [e for e in ledger if e not in mults]
    plan.tracked = len(counters)
    for email, (up, down) in counters.items():
        m = mults.get(email)
        if m is None:
            continue
        try:
            cu, cd = (min(max(int(v or 0), 0), TRAFFIC_MAX) for v in (up, down))
        except (TypeError, ValueError, OverflowError):
            plan.skipped.append(email)
            continue
        led = ledger.get(email)
        if led is None:
            plan.first.append((email, cu, cd, now))   # first sight: never bill traffic from before
            continue
        last_up, last_down, rem_up, rem_down, raw_total, extra_total = led
        # A counter that went DOWN was reset (renewal / manual reset): all of it is new traffic.
        du = cu - last_up if cu >= last_up else cu
        dd = cd - last_down if cd >= last_down else cd
        if du == 0 and dd == 0:
            if (cu, cd) != (last_up, last_down):   # reset to zero: follow it
                plan.moves.append((email, cu, cd, rem_up, rem_down, raw_total, extra_total, now))
            continue
        su, sd = du * (m[0] - SCALE) + rem_up, dd * (m[0] - SCALE) + rem_down
        # Never past the cap: the panel stores the counters as int64, and PostgreSQL errors on overflow.
        xu, xd = min(su // SCALE, TRAFFIC_MAX - cu), min(sd // SCALE, TRAFFIC_MAX - cd)
        if xu or xd:
            plan.credits.append((email, xu, xd))
        # The high-water mark includes our own credit, so it is never counted as traffic.
        plan.moves.append((email, cu + xu, cd + xd, su % SCALE, sd % SCALE,
                           min(raw_total + du + dd, TRAFFIC_MAX), min(extra_total + xu + xd, TRAFFIC_MAX), now))
        st = plan.per_ib.setdefault(m[1], [0, 0, 0])
        st[0] += 1
        st[1] += du + dd
        st[2] += xu + xd
    return plan


def read_plan(db, inbound_mults, now, lock):
    mults = client_multipliers(db, inbound_mults)
    counters = db.counters(sorted(inbound_mults), lock) if mults else {}
    return compute_plan(mults, counters, db.ledger(), now)


def bill(db, inbound_mults, dry_run=False):
    """One tick: add floor(new traffic x (k - 1)) to each client's own counters and advance the ledger,
    in ONE transaction. Returns ({inbound id: [clients, raw, extra]}, [emails skipped: unreadable counters]).

    Two phases. First a look with no lock at all: an idle tick (the usual one) ends here having written
    nothing and held nothing. Only if there is something to bill do we open the transaction, re-read the
    counters under their row locks (so the panel cannot slip an update between our read and our write),
    and write the whole tick in a few set-based statements."""
    now, before = int(time.time()), db.stmts
    plan = read_plan(db, inbound_mults, now, lock=False)
    if plan.work and not dry_run:
        db.begin()
        try:
            plan = read_plan(db, inbound_mults, now, lock=True)
            if plan.work:
                db.apply(plan)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    db.last = {"clients": plan.tracked, "statements": db.stmts - before}
    return plan.per_ib, plan.skipped


def prune_ledger(db, inbound_mults):
    """Forget clients that no longer pay a multiplier (run by `remove`, so it holds while the daemon is down)."""
    db.begin()
    try:
        keep = client_multipliers(db, inbound_mults)
        db.ledger_delete([e for e in db.ledger() if e not in keep])
        db.commit()
    except BaseException:
        db.rollback()
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


def db_identity(target):
    """What must stay the same for the open connection to stay valid: the PostgreSQL DSN, or the SQLite file
    itself (a restore replaces it: new inode)."""
    if target.kind == "postgres":
        return (target.kind, target.ref)
    try:
        st = os.stat(target.ref)
    except FileNotFoundError:
        raise XMError(f"database not found: {target.ref} (is 3X-UI installed? PostgreSQL panels: see `xui-mult db`)")
    return (target.kind, target.ref, st.st_dev, st.st_ino)


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
    db, db_id, target = None, None, None
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

        # --- DB (re)connect: follows the panel's own settings, survives panel / PostgreSQL restarts and
        # SQLite restores (file replaced -> new inode)
        db_err, busy, err, skipped = "", False, "", []
        try:
            target = resolve_target(cfg)
            ident = db_identity(target)
            if db is None or ident != db_id:
                if db is not None:
                    log.warning("database changed or was replaced — reconnecting")
                    db.close()
                    db = None
                db, db_id = db_connect(target.ref), ident
                log.info("database: %s (%s)", db.describe(), target.source)
        except (XMError, OSError) + DB_ERRORS as e:
            db_err, db = str(e), None
            log.error("database: %s", e)

        if db is not None:   # also with no multiplied inbound: bill() forgets clients that left
            try:
                per_ib, skipped = bill(db, cfg["inbounds"], args.dry_run)
                if per_ib:
                    names = dict(db.rows("SELECT id, remark FROM inbounds"))
                    for ib, (n, raw, extra) in sorted(per_ib.items()):
                        totals[0] += raw
                        totals[1] += extra
                        log.info("inbound #%s %s %s: %d client(s) used %s -> %s%s extra", ib, names.get(ib) or "",
                                 fmt_k(cfg["inbounds"][ib]), n, human(raw), "would bill " if args.dry_run else "+",
                                 human(extra))
                if set(skipped) - skipped_seen:
                    log.warning("not billed, unreadable traffic counters: %s", ", ".join(sorted(skipped)))
                skipped_seen = set(skipped)
            except DB_ERRORS as e:
                err, busy = str(e).strip(), db.is_busy(e)
                if busy:
                    log.warning("%s (nothing lost, billed in full next tick)", err)
                else:
                    log.error("database: %s", err)
                if not busy and db.needs_reconnect(e):   # connection lost / corrupt file: reconnect next tick
                    db_err = err
                    db.close()
                    db = None
            except Exception as e:  # noqa: BLE001 — the daemon must survive anything and retry
                err = repr(e)
                log.exception("tick failed (nothing written, retried next tick)")

        status = {"pid": os.getpid(), "version": VERSION, "started": int(started), "last_tick": int(time.time()),
                  "tick_ms": int((time.time() - t0) * 1000), "inbounds": len(cfg["inbounds"]),
                  "backend": target.kind if target else "", "db_error": db_err, "db_busy": busy, "error": err,
                  "skipped": sorted(skipped), "clients": db.last["clients"] if db else 0,
                  "statements": db.last["statements"] if db else 0,
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
    if db:
        db.close()
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


def inbound_client_counts(db):
    """{inbound id: (active clients, all clients)}"""
    rows = db.rows("""SELECT ci.inbound_id, COUNT(*),
                             SUM(CASE WHEN c.enable AND COALESCE(t.enable, TRUE) THEN 1 ELSE 0 END)
                      FROM client_inbounds ci JOIN clients c ON c.id = ci.client_id
                      LEFT JOIN client_traffics t ON t.email = c.email
                      GROUP BY ci.inbound_id""")
    return {ib: (int(active or 0), int(total)) for ib, total, active in rows}


def mixed_clients(db, inbound_mults, mults):
    """{inbound id: n} clients billed at that inbound's multiplier that are also on a lower-multiplier
    inbound (3X-UI can't split their traffic by inbound, so all of it is multiplied)."""
    out = {}
    if not mults:
        return out
    for email, ib in db.rows("SELECT c.email, ci.inbound_id FROM clients c "
                             "JOIN client_inbounds ci ON ci.client_id = c.id"):
        m = mults.get(email)
        if m and mult_fp(inbound_mults.get(ib, 1.0)) < m[0]:
            out.setdefault(m[1], set()).add(email)
    return {ib: len(s) for ib, s in out.items()}


def op_set(inbound_id, mult):
    k = parse_mult(mult)
    with open_db() as db:
        ib = inbound_row(db, inbound_id)
        if not ib:
            raise XMError(f"inbound {inbound_id} not found — see `xui-mult list`")
        with config_txn() as cfg:
            old = cfg["inbounds"].get(inbound_id)
            cfg["inbounds"][inbound_id] = k
        reload_daemon()
        n = inbound_client_counts(db).get(inbound_id, (0, 0))[1]
    what = f"{fmt_k(old)} → {fmt_k(k)}" if old else fmt_k(k)
    ok(f"Inbound #{inbound_id} {plain(ib['remark'] or '')}: {C.title(what)}. Its {n} client(s), and any added "
       f"later, pay {fmt_k(k)} on traffic from now on.")


def op_remove(inbound_id):
    with config_txn() as cfg:
        if inbound_id not in cfg["inbounds"]:
            raise XMError(f"inbound {inbound_id} has no multiplier")
        del cfg["inbounds"][inbound_id]
    with open_db(cfg) as db:
        prune_ledger(db, cfg["inbounds"])
    reload_daemon()
    ok(f"Inbound #{inbound_id} is back to 1.00x (traffic already billed stays billed)")


def _remark_style(dim):
    def style(t):
        badge = "[DISABLED]"
        rest = t[len(badge):] if t.startswith(badge) else t
        rest = dim(rest) if dim else rest
        return C.dim_red(badge) + rest if t.startswith(badge) else rest
    return style


def op_list():
    cfg = load_config()
    with open_db(cfg) as db:
        mults = client_multipliers(db, cfg["inbounds"])
        counts = inbound_client_counts(db)
        extra = {}
        for email, e in db.rows("SELECT email, extra_total FROM xui_mult_ledger"):
            if email in mults:
                extra[mults[email][1]] = extra.get(mults[email][1], 0) + int(e or 0)
        inbounds = all_inbounds(db)
        mixed = mixed_clients(db, cfg["inbounds"], mults)
    if not inbounds:
        info("the panel has no inbounds yet")
        return
    rows = []
    for ib in inbounds:
        k = cfg["inbounds"].get(ib["id"])
        dim = None if k else C.dim                    # inbounds without a multiplier are greyed out
        active, total = counts.get(ib["id"], (0, 0))
        rows.append([(str(ib["id"]), C.bold if k else dim),
                     (("" if ib["enable"] else "[DISABLED] ") + plain(ib["remark"] or ""), _remark_style(dim)),
                     (f"{ib['protocol']}:{ib['port']}", dim),
                     (f"{active}/{total}", dim),
                     (f"[{fmt_k(k)}]", C.title) if k else ("1.00x", C.dim),
                     (f"+{human(extra.get(ib['id'], 0))}", C.yellow) if k else ("—", C.dim)])
    print(table(["ID", "REMARK", "PROTOCOL:PORT", "CLIENTS", "MULT", "EXTRA BILLED"], rows,
                right={0, 3, 4, 5}, shrink=(1, 2, 5)))
    width = term_width() - 2
    for line in wrap("CLIENTS = active/total · EXTRA BILLED = added by xui-mult since the multiplier was set",
                     width):
        print(C.dim(line))
    notes = [f"inbound #{i} has a multiplier but no longer exists — `xui-mult remove {i}`"
             for i in cfg["inbounds"] if not any(ib["id"] == i for ib in inbounds)]
    notes += [f"inbound #{ib}: {n} client(s) are also on a lower-multiplier inbound. 3X-UI keeps one traffic "
              f"counter per client, so ALL their traffic is billed {fmt_k(cfg['inbounds'][ib])}."
              for ib, n in sorted(mixed.items())]
    for note in notes:
        parts = wrap(note, width)
        print(C.yellow("! ") + parts[0] + "".join("\n  " + p for p in parts[1:]))


def op_db(action="show", value=None):
    """Which database xui-mult uses: show it (and test the connection), pin one, or follow the panel again."""
    if action == "set":
        if not value:
            raise XMError("usage: xui-mult db set <SQLite file | PostgreSQL DSN>")
        with db_connect(value) as db:            # refuse to pin something that does not work
            desc = db.describe()
        with config_txn() as cfg:
            cfg["db"] = value
        reload_daemon()
        ok(f"Pinned: {desc}")
        return 0
    if action == "auto":
        with config_txn() as cfg:
            cfg["db"] = "auto"
        reload_daemon()
        ok("Following the panel's own database settings (XUI_DB_TYPE / XUI_DB_DSN)")
    target = resolve_target(load_config())
    shown = mask_dsn(target.ref) if target.kind == "postgres" else target.ref
    info(f"Using {target.kind} · {shown} ({target.source})")
    with db_connect(target.ref) as db:
        ok(db.describe())
        ok(f"{len(all_inbounds(db))} inbound(s), {sum(t for _, t in inbound_client_counts(db).values())} client link(s)")
    return 0


def op_drop_ledger():
    """Uninstall helper: the ledger is useless without the service."""
    with open_db() as db:
        db.run("DROP TABLE IF EXISTS xui_mult_ledger")
        db.run("DROP TABLE IF EXISTS tunnel_multiplier_ledger")     # from 1.x


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
    rows, problems, width = [], 0, card_width()

    def line(icon, text):
        parts = wrap(text, width - 2)
        rows.append(f"{icon} {parts[0]}")
        rows.extend(f"  {p}" for p in parts[1:])

    def check(cond, good, badmsg, hint=""):
        nonlocal problems
        if cond:
            line(C.green("✔"), good)
        else:
            problems += 1
            line(C.red("✖"), badmsg + (f" → {hint}" if hint else ""))

    def show(code):
        rows.append(SEP)
        if problems:
            line(C.yellow("!"), f"{problems} problem(s)")
        else:
            line(C.green("✔"), "All checks passed")
        print(card(f"xui-mult {VERSION} · status", rows, footer=f"verified on 3X-UI {PANEL_VERIFIED}"))
        return code

    try:
        cfg = load_config()
        check(True, f"Config {CONF_PATH} · {len(cfg['inbounds'])} multiplied inbound(s)", "")
    except (XMError, ValueError, OSError) as e:
        check(False, "", f"Config: {e}")
        return show(1)
    active = service_active()
    check(active, f"Service {C.green('● running')}", f"Service {C.red('● stopped')}", "systemctl start xui-mult")
    st = read_status()
    if st:
        age = int(time.time() - st["last_tick"])
        check(age < max(60, 5 * cfg["interval"]), f"Last tick {age}s ago ({st['tick_ms']} ms)",
              f"Last tick {age}s ago — daemon stuck?", "xui-mult logs")
        check(not st.get("error") or st.get("db_busy"), "Last tick billed without errors",
              f"Last tick failed: {st.get('error')}", "xui-mult logs")
        if st.get("db_busy"):
            line(C.yellow("!"), "The last tick found the database locked by the panel (billed in full on the next one)")
        if st.get("clients"):
            line(C.cyan("•"), f"Tracking {st['clients']} client(s) · {st.get('statements', 0)} statement(s) per tick")
        check(not st.get("skipped"), "All clients' counters readable",
              f"Not billed, unreadable counters: {', '.join(st.get('skipped') or [])}", "fix them in the panel")
    elif active:
        line(C.yellow("!"), "No heartbeat yet")
    try:
        target = resolve_target(cfg)
        db = db_connect(target.ref)
        check(True, f"Database {db.describe()} ({target.source})", "")
    except (XMError,) + DB_ERRORS as e:
        check(False, "", f"Database: {e}")
        return show(1)
    ver = (sh(["/usr/local/x-ui/x-ui", "-v"], capture_output=True, text=True, timeout=5,
              stdin=subprocess.DEVNULL).stdout or "").strip()
    if ver and PANEL_VERIFIED.lstrip("v") in ver:
        line(C.green("✔"), f"Panel 3X-UI {ver}")
    elif ver:
        line(C.yellow("!"), f"Panel 3X-UI {ver} — not verified (xui-mult was verified on {PANEL_VERIFIED})")
    counts = inbound_client_counts(db)
    for i, k in cfg["inbounds"].items():
        ib = inbound_row(db, i)
        check(ib is not None, f"Inbound #{i} {plain((ib or {}).get('remark') or '')} {C.title(f'[{fmt_k(k)}]')} · "
              f"{counts.get(i, (0, 0))[1]} client(s)", f"Inbound #{i} [{fmt_k(k)}] no longer exists",
              f"xui-mult remove {i}")
    db.close()
    if st and st.get("extra_since_start"):
        rows.append(SEP)
        line(C.cyan("•"), f"Since the service started: {human(st['raw_since_start'])} used on multiplied inbounds "
                          f"→ {C.yellow('+' + human(st['extra_since_start']))} extra billed")
    return show(0 if not problems else 1)


def op_logs(follow=False, lines=100):
    cmd = ["journalctl", "-u", SERVICE, "-n", str(lines), "--no-pager"]
    if follow:
        cmd = ["journalctl", "-u", SERVICE, "-f", "-n", str(lines)]
    with contextlib.suppress(KeyboardInterrupt):
        sh(cmd)


# =================================================================================== menu

MENU = [
    ("1", "Set / edit multiplier for an inbound"),
    ("2", "List inbounds & multipliers"),
    ("3", "Remove multiplier from an inbound"),
    ("4", "Service status & logs"),
    ("0", "Exit"),
]


def dashboard():
    """The menu screen: a card with the service state, the multiplied inbounds and the options."""
    try:
        ks = load_config()["inbounds"]
    except (XMError, ValueError, OSError):
        ks = None
    st, width = read_status(), card_width()
    svc = C.green("● running") if service_active() else C.red("● stopped")
    tick = f"{int(time.time() - st['last_tick'])}s ago" if st else "—"
    extra = (C.yellow("+" + human(st.get("extra_since_start", 0))) + C.dim(" since start")) if st else "—"
    count = "config error" if ks is None else f"{len(ks)} inbound(s)"
    pairs = [("Service", svc), ("Last tick", tick), ("Multiplied", count), ("Extra billed", extra)]
    rows = [C.dim("Inbound traffic multipliers for 3X-UI"), SEP]
    half = width // 2
    kv = lambda k, v, w: pad(C.dim(k), 13) + pad(v, w - 13)  # noqa: E731
    if width >= 60:
        rows += [kv(*pairs[i], half) + kv(*pairs[i + 1], width - half) for i in (0, 2)]
    else:
        rows += [kv(k, v, width) for k, v in pairs]
    if st and st.get("backend"):
        rows.append(kv("Database", {"postgres": "PostgreSQL", "sqlite": "SQLite"}.get(st["backend"], st["backend"])
                       + C.dim(f" · {st.get('tick_ms', 0)} ms/tick · {st.get('clients', 0)} clients"), width))
    if ks:
        rows.append(kv("Inbounds", C.title(fit(" · ".join(f"#{i} {fmt_k(k)}" for i, k in ks.items()), width - 13)),
                       width))
    rows.append(SEP)
    rows += [f" {C.dim(k) if k == '0' else C.title(k)}  {C.dim(label) if k == '0' else label}" for k, label in MENU]
    print(card(f"xui-mult v{VERSION}", rows))


def pick(ids, what="inbound"):
    def validate(v):
        i = int(v)
        if i not in ids:
            raise ValueError(f"no {what} #{i}")
        return i
    return validate


def menu():
    first = True
    while True:
        if not first and sys.stdout.isatty():
            print("\033[2J\033[H", end="")      # redraw on a clean screen (the first one keeps what was above)
        first = False
        dashboard()
        try:
            choice = input(C.bold("\n Choose › ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        try:
            if choice == "0":
                return 0
            elif choice == "1":
                op_list()
                cfg = load_config()
                with open_db(cfg) as db:
                    ids = {ib["id"] for ib in all_inbounds(db)}
                ib = ask("\nInbound ID " + C.dim("(Ctrl+C to cancel)"), validate=pick(ids))
                op_set(ib, ask("Multiplier", cfg["inbounds"].get(ib) or 1.2, validate=parse_mult))
            elif choice == "2":
                op_list()
            elif choice == "3":
                configured = load_config()["inbounds"]
                if not configured:
                    info("no inbound has a multiplier yet")
                else:
                    op_list()
                    op_remove(ask("\nInbound ID " + C.dim("(Ctrl+C to cancel)"),
                                  validate=pick(set(configured), "multiplied inbound")))
            elif choice == "4":
                op_status()
                if confirm("\nFollow live billing? (Ctrl+C to go back)", True):
                    op_logs(follow=True, lines=20)
                continue
            else:
                bad("unknown option")
        except KeyboardInterrupt:
            print()
        except (XMError, OSError) + DB_ERRORS as e:
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
    s = sp.add_parser("db", help="which database is used: show (default), `set <SQLite file | PostgreSQL DSN>`, `auto`")
    s.add_argument("action", nargs="?", choices=("show", "set", "auto"), default="show")
    s.add_argument("value", nargs="?")
    sp.add_parser("drop-ledger", help=argparse.SUPPRESS)
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
    if (args.cmd in (None, "set", "remove", "run", "drop-ledger")
            or (args.cmd == "db" and args.action != "show")) and os.geteuid() != 0:
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
        elif args.cmd == "db":
            return op_db(args.action, args.value)
        elif args.cmd == "drop-ledger":
            op_drop_ledger()
        elif args.cmd == "logs":
            op_logs(args.follow, args.n)
    except KeyboardInterrupt:
        print()
        return 130
    except (XMError, OSError) + DB_ERRORS as e:
        bad(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
