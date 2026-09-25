#!/usr/bin/env python3
"""
xui-mult — per-inbound traffic multiplier for 3X-UI (MHSanaei), via "shadow clients".

Every user who should pay extra for a tunnel inbound gets a shadow client "<email>_tun" on that
inbound (same UUID/password, own email so Xray counts it separately). A background service bills
the shadow's bytes x multiplier to the real ("master") client:

  * billing   : one BEGIN IMMEDIATE SQLite transaction per tick; atomic `up = MIN(up + ?, cap)` on the
                master (the exact form 3X-UI v3.8.5 uses) + a high-water-mark ledger stored inside
                x-ui.db. The shadow's row is never written. Fractions are carried, so billed == floor(raw*k).
  * state     : read from the DB, written ONLY through the panel REST API (so Xray is updated live):
                enable/disable, expiry, fail-safe quota and IP limit are mirrored master -> shadow;
                the shadow's counter is reset when the master is renewed.

Verified against 3X-UI v3.8.5 (tag 7ef22f9). Standard library only. Run `xui-mult` for the menu.
"""

import argparse
import contextlib
import fcntl
import ipaddress
import json
import logging
import math
import os
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERSION = "1.1.0"
PANEL_VERIFIED = "v3.8.5"
SCALE = 1000                   # fixed-point multiplier: 1200 == 1.200x
TRAFFIC_MAX = (1 << 63) - 1    # same cap as the panel's ClampedAddExpr
BUSY_TIMEOUT_MS = 10000        # same as the panel's DSN (_busy_timeout=10000)
LINK_REMARK_PREFIX = "xui-mult:"
SERVICE = "xui-mult"

CONF_DIR = os.environ.get("XUI_MULT_CONF_DIR", "/etc/xui-mult")
RUN_DIR = os.environ.get("XUI_MULT_RUN_DIR", "/run/xui-mult")
CONF_PATH = os.path.join(CONF_DIR, "config.json")
STATUS_PATH = os.path.join(RUN_DIR, "status.json")
DEFAULT_DB = "/etc/x-ui/x-ui.db"

DEFAULT_CONFIG = {
    "db": DEFAULT_DB,
    "api_url": "",          # e.g. https://127.0.0.1:2053/secretpath
    "api_token": "",
    "link_host": "",        # public host used inside generated share links (Host header)
    "api_host": "auto",     # Host header for API calls; auto = the panel's webDomain (other hosts get 403)
    "verify_tls": "auto",   # auto = skip verification only for loopback URLs
    "interval": 7,
    "sub_link": True,       # put the tunnel link into the master's subscription
    "pairs": [],            # {"master","shadow","multiplier","inbound_id"}
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
    k = float(v)
    if not 1.0 <= k <= 10.0:
        raise ValueError("multiplier must be between 1.0 and 10.0")
    return round(k, 3)


def mult_fp(k):
    return round(float(k) * SCALE)


def fmt_expiry(ms):
    if not ms:
        return "never"
    if ms < 0:
        return f"{-ms // 86400000}d after first use"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


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
    cfg = dict(DEFAULT_CONFIG)
    cfg["pairs"] = []
    if os.path.exists(CONF_PATH):
        with open(CONF_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    validate_config(cfg)
    return cfg


def validate_config(cfg):
    seen = set()
    for p in cfg["pairs"]:
        for k in ("master", "shadow", "multiplier", "inbound_id"):
            if k not in p:
                raise XMError(f"config: pair {p} is missing '{k}'")
        parse_mult(p["multiplier"])
        if p["master"] == p["shadow"]:
            raise XMError(f"config: {p['shadow']}: master and shadow must differ")
        if p["shadow"] in seen:
            raise XMError(f"config: shadow {p['shadow']} listed twice")
        seen.add(p["shadow"])
    if float(cfg.get("interval", 7)) < 2:
        raise XMError("config: interval must be >= 2 seconds")


@contextlib.contextmanager
def config_txn():
    """Exclusive, atomic read-modify-write of the config file (safe against two CLIs at once)."""
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(os.path.join(CONF_DIR, ".config.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        cfg = load_config()
        yield cfg
        validate_config(cfg)
        atomic_write(CONF_PATH, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def find_pairs(cfg, email):
    return [p for p in cfg["pairs"] if email in (p["master"], p["shadow"])]


def one_pair(cfg, email):
    ps = find_pairs(cfg, email)
    if not ps:
        raise XMError(f"{email} is not managed by xui-mult")
    if len(ps) > 1:
        raise XMError(f"{email} has several tunnels ({', '.join(p['shadow'] for p in ps)}); use the shadow email")
    return ps[0]


# =================================================================================== database

LEDGER_DDL = """CREATE TABLE IF NOT EXISTS tunnel_multiplier_ledger (
    shadow           TEXT PRIMARY KEY,
    last_up          INTEGER NOT NULL,
    last_down        INTEGER NOT NULL,
    rem_up           INTEGER NOT NULL DEFAULT 0,
    rem_down         INTEGER NOT NULL DEFAULT 0,
    raw_total        INTEGER NOT NULL DEFAULT 0,
    billed_total     INTEGER NOT NULL DEFAULT 0,
    last_master_used INTEGER,
    mirrored_expiry  INTEGER,
    updated_at       INTEGER NOT NULL
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
    if not {"email", "up", "down", "enable", "total", "expiry_time"} <= cols:
        conn.close()
        raise XMError("client_traffics schema differs from 3X-UI v3.8.5 — refusing to run")
    conn.execute(LEDGER_DDL)
    if "mirrored_expiry" not in {r[1] for r in conn.execute("PRAGMA table_info(tunnel_multiplier_ledger)")}:
        conn.execute("ALTER TABLE tunnel_multiplier_ledger ADD COLUMN mirrored_expiry INTEGER")  # from 1.0.x
    return conn


def panel_domain(path):
    """The panel's webDomain setting. When it is set, 3X-UI answers 403 to any other Host header."""
    if not os.path.exists(path):
        return ""
    with contextlib.suppress(sqlite3.Error):
        conn = sqlite3.connect(path, timeout=5)
        try:
            r = q1(conn, "SELECT value FROM settings WHERE key = 'webDomain'")
            return (r[0] or "").strip() if r else ""
        finally:
            conn.close()
    return ""


def db_identity(path):
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def q1(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def client_row(conn, email):
    """Merged view of a client: clients record + traffic row. None if missing."""
    r = q1(conn, """SELECT c.id, c.email, c.uuid, c.password, c.enable, c.total_gb, c.expiry_time,
                           c.limit_ip, c.sub_id, t.up, t.down, t.enable, t.total
                    FROM clients c LEFT JOIN client_traffics t ON t.email = c.email
                    WHERE c.email = ?""", (email,))
    if not r:
        return None
    keys = ("id", "email", "uuid", "password", "rec_enable", "total_gb", "expiry_time", "limit_ip",
            "sub_id", "up", "down", "ct_enable", "ct_total")
    d = dict(zip(keys, r))
    d["up"], d["down"] = d["up"] or 0, d["down"] or 0
    d["used"] = d["up"] + d["down"]
    d["enabled"] = bool(d["rec_enable"]) and (d["ct_enable"] is None or bool(d["ct_enable"]))
    return d


def client_inbound_ids(conn, client_id):
    return [r[0] for r in conn.execute("SELECT inbound_id FROM client_inbounds WHERE client_id = ?", (client_id,))]


def inbound_row(conn, inbound_id):
    r = q1(conn, "SELECT id, remark, protocol, port, tag, enable FROM inbounds WHERE id = ?", (inbound_id,))
    return dict(zip(("id", "remark", "protocol", "port", "tag", "enable"), r)) if r else None


def all_inbounds(conn):
    rows = conn.execute("SELECT id, remark, protocol, port, tag, enable FROM inbounds ORDER BY id").fetchall()
    return [dict(zip(("id", "remark", "protocol", "port", "tag", "enable"), r)) for r in rows]


def ledger_row(conn, shadow):
    r = q1(conn, """SELECT last_up, last_down, rem_up, rem_down, raw_total, billed_total, last_master_used,
                           mirrored_expiry, updated_at FROM tunnel_multiplier_ledger WHERE shadow = ?""", (shadow,))
    keys = ("last_up", "last_down", "rem_up", "rem_down", "raw_total", "billed_total", "last_master_used",
            "mirrored_expiry", "updated_at")
    return dict(zip(keys, r)) if r else None


def ledger_init(conn, shadow, up=0, down=0, master_used=None, expiry=None):
    """Baseline for a brand-new shadow client. Overwrites an orphan row left by an earlier pair of the
    same name (only called right after the panel created the client, so no live row can exist)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""INSERT INTO tunnel_multiplier_ledger(shadow,last_up,last_down,last_master_used,mirrored_expiry,
                                                             updated_at) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(shadow) DO UPDATE SET last_up=excluded.last_up, last_down=excluded.last_down,
                            rem_up=0, rem_down=0, raw_total=0, billed_total=0,
                            last_master_used=excluded.last_master_used, mirrored_expiry=excluded.mirrored_expiry,
                            updated_at=excluded.updated_at""",
                     (shadow, up, down, master_used, expiry, int(time.time())))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def ledger_delete(conn, shadow):
    conn.execute("DELETE FROM tunnel_multiplier_ledger WHERE shadow = ?", (shadow,))


def account(conn, master, shadow, mult, dry_run=False):
    """Bill the growth of shadow's counters since the last tick to master, scaled by mult/SCALE.
    One BEGIN IMMEDIATE transaction: the panel (which also uses immediate transactions) can never
    interleave. Returns (raw_delta, billed) or None if a row is missing."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        s = q1(conn, "SELECT up, down FROM client_traffics WHERE email = ?", (shadow,))
        if s is None:
            conn.execute("ROLLBACK")
            return None
        # Defensive: a corrupted or REAL-typed counter must not crash billing or leave the int64 range.
        cur_up, cur_down = (min(max(int(v or 0), 0), TRAFFIC_MAX) for v in s)
        now = int(time.time())
        led = q1(conn, "SELECT last_up,last_down,rem_up,rem_down FROM tunnel_multiplier_ledger WHERE shadow=?",
                 (shadow,))
        inserted = led is None
        if inserted:
            # Unknown shadow (e.g. ledger lost after a DB restore): never bill history we can't attribute.
            conn.execute("INSERT INTO tunnel_multiplier_ledger(shadow,last_up,last_down,updated_at) VALUES(?,?,?,?)",
                         (shadow, cur_up, cur_down, now))
            led = (cur_up, cur_down, 0, 0)
            log.info("%s: ledger initialised at current counters (up=%d down=%d)", shadow, cur_up, cur_down)
        last_up, last_down, rem_up, rem_down = led
        # A counter that went DOWN was reset (renewal / manual reset): all of it is new traffic.
        d_up = cur_up - last_up if cur_up >= last_up else cur_up
        d_down = cur_down - last_down if cur_down >= last_down else cur_down

        if d_up == 0 and d_down == 0:
            if dry_run or (not inserted and (cur_up, cur_down) == (last_up, last_down)):
                conn.execute("ROLLBACK")
                return (0, 0)
            conn.execute("UPDATE tunnel_multiplier_ledger SET last_up=?, last_down=?, updated_at=? WHERE shadow=?",
                         (cur_up, cur_down, now, shadow))
            conn.execute("COMMIT")  # COMMIT, not ROLLBACK: a first-sight ledger row must persist
            return (0, 0)

        su, sd = d_up * mult + rem_up, d_down * mult + rem_down
        bill_up, bill_down = min(su // SCALE, TRAFFIC_MAX), min(sd // SCALE, TRAFFIC_MAX)
        if dry_run:
            conn.execute("ROLLBACK")
            return (d_up + d_down, bill_up + bill_down)

        cur = conn.execute("UPDATE client_traffics SET up = MIN(up + ?, ?), down = MIN(down + ?, ?) WHERE email = ?",
                           (bill_up, TRAFFIC_MAX, bill_down, TRAFFIC_MAX, master))
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            return None
        conn.execute("""UPDATE tunnel_multiplier_ledger
                        SET last_up=?, last_down=?, rem_up=?, rem_down=?,
                            raw_total = raw_total + ?, billed_total = billed_total + ?, updated_at=?
                        WHERE shadow=?""",
                     (cur_up, cur_down, su % SCALE, sd % SCALE, min(d_up + d_down, TRAFFIC_MAX),
                      min(bill_up + bill_down, TRAFFIC_MAX), now, shadow))
        conn.execute("COMMIT")
        return (d_up + d_down, bill_up + bill_down)
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def detect_panel_url(conn):
    """Build the local API base URL from the panel's own settings table."""
    s = dict(conn.execute("SELECT key, value FROM settings WHERE key IN "
                          "('webPort','webBasePath','webCertFile','webListen','webDomain')").fetchall())
    port = s.get("webPort") or "2053"
    base = (s.get("webBasePath") or "/").strip()
    base = "/" + base.strip("/") if base.strip("/") else ""
    scheme = "https" if s.get("webCertFile") else "http"
    listen = (s.get("webListen") or "").strip()
    host = "127.0.0.1" if listen in ("", "0.0.0.0", "::") else listen
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}{base}", s.get("webDomain") or ""


# =================================================================================== panel API

class ApiError(XMError):
    pass


class PanelAPI:
    """Thin client for /panel/api (3X-UI v3.8.5). All writes to client state go through here."""

    def __init__(self, base_url, token, link_host="", verify_tls="auto", timeout=15, api_host=""):
        if not base_url or not token:
            raise XMError("panel API is not configured — run: xui-mult setup")
        self.base = base_url.rstrip("/")
        self.token = token
        self.link_host = link_host
        self.api_host = api_host
        self.timeout = timeout
        host = urllib.parse.urlsplit(self.base).hostname or ""
        loopback = host == "localhost"
        with contextlib.suppress(ValueError):
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        verify = (not loopback) if verify_tls == "auto" else bool(verify_tls)
        self.ctx = ssl.create_default_context()
        if not verify:  # traffic never leaves the machine on loopback
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _call(self, method, path, payload=None, host=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        host = host or self.api_host
        if host:
            headers["Host"] = host
        req = urllib.request.Request(self.base + "/panel/api" + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ApiError("API token rejected (401) — create a token with full access and run setup")
            if e.code == 403:
                body = b""
                with contextlib.suppress(OSError):
                    body = e.read()
                if b"not permitted" in body:  # monitor / node-sync scoped token
                    raise ApiError(f"API token may not call {path} (403) — create a token with FULL (admin) "
                                   "access and run setup")
                raise ApiError(f"403 for {path} — the panel accepts only its own domain as Host; "
                               "set the panel domain with `xui-mult setup --api-host DOMAIN`")
            if e.code == 404:
                raise ApiError(f"404 for {path} — wrong panel URL / base path?")
            raise ApiError(f"HTTP {e.code} for {path}")
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(f"cannot reach panel at {self.base}: {getattr(e, 'reason', e)}")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(f"{path}: panel returned non-JSON (wrong URL?)")
        if not body.get("success", False):
            raise ApiError(f"{path}: {body.get('msg') or 'panel reported failure'}")
        return body.get("obj")

    # --- reads
    def ping(self):
        # A /clients route: node-sync and monitor tokens can list inbounds but not manage clients,
        # so /inbounds/list would pass setup with a token that can never disable a shadow.
        return self._call("GET", "/clients/list/paged?page=1&pageSize=1")

    def get_client(self, email):
        return self._call("GET", "/clients/get/" + urllib.parse.quote(email, safe=""))

    def client_links(self, email):
        # With a panel domain set, any other Host is refused, so the links are built for that domain.
        obj = self._call("GET", "/clients/links/" + urllib.parse.quote(email, safe=""),
                         host=self.api_host or self.link_host or None)
        return [x for x in (obj or []) if isinstance(x, str) and x.strip()]

    # --- writes
    def add_client(self, client, inbound_ids):
        return self._call("POST", "/clients/add", {"client": client, "inboundIds": list(inbound_ids)})

    def update_client(self, email, client):
        return self._call("POST", "/clients/update/" + urllib.parse.quote(email, safe=""), client)

    def delete_client(self, email):
        return self._call("POST", "/clients/del/" + urllib.parse.quote(email, safe=""))

    def attach(self, email, inbound_ids):
        return self._call("POST", f"/clients/{urllib.parse.quote(email, safe='')}/attach",
                          {"inboundIds": list(inbound_ids)})

    def detach(self, email, inbound_ids):
        return self._call("POST", f"/clients/{urllib.parse.quote(email, safe='')}/detach",
                          {"inboundIds": list(inbound_ids)})

    def set_enabled(self, emails, enabled):
        if not emails:
            return
        path = "/clients/bulkEnable" if enabled else "/clients/bulkDisable"
        res = self._call("POST", path, {"emails": list(emails)})
        # The panel answers success=true even when it skipped some emails; they come back here.
        skipped = [x for x in (res or {}).get("skipped") or [] if isinstance(x, dict)]
        if skipped:
            raise ApiError(f"{path} skipped " + ", ".join(f"{x.get('email')} ({x.get('reason')})" for x in skipped))

    def reset_traffic(self, email):
        return self._call("POST", "/clients/resetTraffic/" + urllib.parse.quote(email, safe=""))

    def set_external_links(self, email, links):
        return self._call("POST", f"/clients/{urllib.parse.quote(email, safe='')}/externalLinks",
                          {"externalLinks": links})


def api_from_config(cfg):
    host = cfg.get("api_host", "auto")
    if host == "auto":
        host = panel_domain(cfg["db"])
    return PanelAPI(cfg["api_url"], cfg["api_token"], cfg.get("link_host", ""), cfg.get("verify_tls", "auto"),
                    api_host=host or "")


# Fields of model.Client (the update/add payload), copied from a ClientRecord (the get payload).
# allowedIPs/keepAlive have other types in the record; omitting them keeps the stored values.
_RECORD_TO_CLIENT = ("security", "password", "flow", "auth", "privateKey", "publicKey", "preSharedKey",
                     "forwardedPorts", "secret", "adTag", "email", "limitIp", "totalGB", "expiryTime", "enable",
                     "tgId", "subId", "group", "comment", "reset", "resetDay", "resetMax", "trafficReset",
                     "trafficResetDay")


def record_to_client(rec):
    c = {k: rec[k] for k in _RECORD_TO_CLIENT if k in rec and rec[k] is not None}
    c["id"] = rec.get("uuid", "")
    rev = rec.get("reverse")
    if isinstance(rev, str) and rev.strip():   # the record stores it as JSON text, the payload as an object
        with contextlib.suppress(ValueError):
            rev = json.loads(rev)
    if isinstance(rev, dict) and rev:
        c["reverse"] = rev
    if rec.get("limitHwid"):
        c["limitHwid"] = rec["limitHwid"]
    return c


def creds_differ(m, s):
    """Only credentials the master actually has must match (vless/vmess: uuid, trojan/ss: password)."""
    return any(m[k] and m[k] != s[k] for k in ("uuid", "password"))


def failsafe_total(master_total, mult):
    """Shadow quota: tunnel alone can never exceed master_quota / k, even if xui-mult is down."""
    return 0 if not master_total else max(1, math.ceil(master_total * SCALE / mult))


# =================================================================================== reconcile (daemon)

def reconcile(conn, cfg, api, stats):
    """Level-triggered: compute desired shadow state from the master and fix differences through the
    API. Idempotent, so a crash or a failed call is simply retried next tick."""
    to_disable, to_enable, errors = [], [], []
    for p in cfg["pairs"]:
        sd_notify("WATCHDOG=1")
        try:
            _reconcile_pair(conn, p, api, stats, to_disable, to_enable)
        except Exception as e:  # noqa: BLE001 — one bad pair must not stop the others
            errors.append(f"{p['shadow']}: {e if isinstance(e, (XMError, sqlite3.Error)) else repr(e)}")
    for emails, en in ((to_disable, False), (to_enable, True)):
        if emails:
            try:
                api.set_enabled(emails, en)
                log.info("%s shadows %s to match master", "enabled" if en else "disabled", emails)
            except XMError as e:
                errors.append(f"bulk {'enable' if en else 'disable'}: {e}")
    if errors:
        raise XMError("; ".join(errors[:5]) + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""))


def reset_shadow(conn, api, master, shadow, mult):
    """Zero the shadow's counters: bill everything up to now, reset through the panel, then bill again
    so the ledger sees the drop at once (by the next tick new traffic could outgrow the old counter
    and hide the reset)."""
    account(conn, master, shadow, mult)
    api.reset_traffic(shadow)
    account(conn, master, shadow, mult)


def _reconcile_pair(conn, p, api, stats, to_disable, to_enable):
    master, shadow, mult = p["master"], p["shadow"], mult_fp(p["multiplier"])
    m, s = client_row(conn, master), client_row(conn, shadow)
    if not s:
        stats["missing"].add(shadow)
        return
    if not m:
        # Fail closed: a shadow whose master was deleted or renamed must not keep free tunnel access.
        stats["missing"].add(master)
        if s["enabled"]:
            to_disable.append(shadow)
        return
    try:
        _sync_pair_settings(conn, master, shadow, mult, m, s, api)
    finally:
        # 4) mirror enable, even if a sync step above failed: this is what blocks a user on the tunnel.
        #    Shadows are fully managed: to block a user, disable the MASTER. A master whose quota is used
        #    up is treated as disabled now instead of one panel tick later.
        m, s = client_row(conn, master) or m, client_row(conn, shadow) or s
        m_depleted = bool(m["ct_total"]) and m["used"] >= m["ct_total"]
        want_enabled = m["enabled"] and not m_depleted
        s_depleted = bool(s["total_gb"]) and s["used"] >= s["total_gb"]
        if not want_enabled and s["enabled"]:
            to_disable.append(shadow)
        elif want_enabled and not s["enabled"]:
            if s_depleted:  # fail-safe tripped while master is fine -> master was renewed
                reset_shadow(conn, api, master, shadow, mult)
            to_enable.append(shadow)


def _sync_pair_settings(conn, master, shadow, mult, m, s, api):
    led = ledger_row(conn, shadow) or {}

    # 1) "Start after first use" (negative expiry = duration). If the tunnel was used first, the panel
    #    started the SHADOW's clock; start the master's at the same deadline. Otherwise step 2 would put
    #    the duration back on the shadow and the deadline would move forward on every use, forever.
    #    mirrored_expiry tells a clock started by use apart from a stale value the admin just replaced.
    m_exp, s_exp = m["expiry_time"] or 0, s["expiry_time"] or 0
    if m_exp < 0 < s_exp and led.get("mirrored_expiry") == m_exp:
        rec = api.get_client(master)["client"]
        api.update_client(master, dict(record_to_client(rec), expiryTime=s_exp))
        log.info("%s: first use was through the tunnel -> expiry set to %s", master, fmt_expiry(s_exp))
        m_exp = s_exp

    # 2) mirror settings (only when they differ -> no API traffic in steady state)
    want = {"expiryTime": m_exp, "totalGB": failsafe_total(m["total_gb"], mult), "limitIp": m["limit_ip"] or 0}
    have = {"expiryTime": s_exp, "totalGB": s["total_gb"] or 0, "limitIp": s["limit_ip"] or 0}
    if want != have:
        rec = api.get_client(shadow)["client"]
        client = record_to_client(rec)
        client.update(want)
        api.update_client(shadow, client)
        log.info("%s: synced %s", shadow, {k: v for k, v in want.items() if have[k] != v})
        s = client_row(conn, shadow)
    if led.get("mirrored_expiry") != m_exp:
        conn.execute("UPDATE tunnel_multiplier_ledger SET mirrored_expiry=? WHERE shadow=?", (m_exp, shadow))

    # 3) master renewed/reset (its usage went down) -> bill what's left, then reset the shadow too,
    #    otherwise its fail-safe quota would cut the tunnel early in the new period. Runs after step 2
    #    so the shadow already has the new expiry when the reset re-enables it.
    last_mu = led.get("last_master_used")
    if last_mu is not None and m["used"] < last_mu and s["used"] > 0:
        reset_shadow(conn, api, master, shadow, mult)
        log.info("%s renewed -> reset %s", master, shadow)
    conn.execute("UPDATE tunnel_multiplier_ledger SET last_master_used=? WHERE shadow=?", (m["used"], shadow))


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
    api_errors, last_api_err, api = 0, "", None
    started = time.time()
    log.info("xui-mult %s started (panel verified: %s)", VERSION, PANEL_VERIFIED)
    sd_notify("READY=1")

    while not _stop:
        t0 = time.time()
        stats = {"missing": set(), "billed": 0, "raw": 0}
        # --- config hot-reload (CLI edits, SIGHUP)
        try:
            mt = os.path.getmtime(CONF_PATH) if os.path.exists(CONF_PATH) else None
            if _reload or mt != cfg_mtime:
                cfg, cfg_mtime, _reload = load_config(), mt, False
                api = None
                log.info("config loaded: %d pair(s)", len(cfg["pairs"]))
        except (XMError, ValueError, OSError) as e:
            log.error("config error, keeping previous config: %s", e)

        # --- DB (re)connect: survives panel restarts and DB restores (file replaced -> new inode)
        db_err = ""
        try:
            ident = db_identity(cfg["db"])
            if conn is None or ident != conn_id:
                if conn is not None:
                    log.warning("database file was replaced — reconnecting")
                    conn.close()
                conn, conn_id = db_connect(cfg["db"]), ident
        except (XMError, OSError, sqlite3.Error) as e:
            db_err = str(e)
            conn = None
            log.error("database: %s", e)

        busy = False
        if conn is not None:
            # --- billing (pure DB, atomic: a failure anywhere leaves both master and ledger untouched)
            for p in cfg["pairs"]:
                sd_notify("WATCHDOG=1")
                try:
                    res = account(conn, p["master"], p["shadow"], mult_fp(p["multiplier"]), args.dry_run)
                    if res is None:
                        stats["missing"].add(p["shadow"])
                    elif res[0]:
                        stats["raw"] += res[0]
                        stats["billed"] += res[1]
                        log.info("%s -> %s: raw=%s billed=%s (x%s)", p["shadow"], p["master"],
                                 res[0], res[1], p["multiplier"])
                except sqlite3.OperationalError as e:
                    log.warning("%s: %s (nothing lost, retried next tick)", p["shadow"], e)
                    if "locked" in str(e) or "busy" in str(e):
                        busy = True  # the lock is database-wide: don't wait busy_timeout again for every pair
                        break
                except sqlite3.DatabaseError as e:
                    if type(e) is not sqlite3.DatabaseError:  # integrity/programming errors: this pair only
                        log.error("%s: billing failed: %s (skipped this tick)", p["shadow"], e)
                        continue
                    db_err = str(e)  # corrupt or replaced file: reconnect next tick
                    log.error("database: %s — reconnecting", e)
                    conn.close()
                    conn = None
                    break
                except Exception:  # noqa: BLE001 — one bad pair must not stop billing for everyone else
                    log.exception("%s: billing failed (skipped this tick)", p["shadow"])
            # --- state mirroring (API)
            if conn is not None and not busy and not args.dry_run and cfg["pairs"]:
                try:
                    api = api or api_from_config(cfg)
                    reconcile(conn, cfg, api, stats)
                    api_errors, last_api_err = 0, ""
                except Exception as e:  # noqa: BLE001 — the daemon must keep billing whatever the API does
                    api_errors += 1
                    last_api_err = str(e)
                    lvl = logging.ERROR if api_errors in (1, 10) or api_errors % 100 == 0 else logging.DEBUG
                    log.log(lvl, "state sync failed (%d in a row): %s", api_errors, e)
        if stats["missing"]:
            log.warning("missing clients: %s", ", ".join(sorted(stats["missing"])))

        status = {"pid": os.getpid(), "version": VERSION, "started": int(started), "last_tick": int(time.time()),
                  "tick_ms": int((time.time() - t0) * 1000), "pairs": len(cfg["pairs"]),
                  "db_error": db_err, "db_busy": busy, "api_errors": api_errors, "api_error": last_api_err,
                  "missing": sorted(stats["missing"]), "dry_run": args.dry_run}
        with contextlib.suppress(OSError):
            atomic_write(STATUS_PATH, json.dumps(status), 0o644)
        sd_notify("WATCHDOG=1\nSTATUS=" + (f"api errors: {api_errors}" if api_errors else f"{len(cfg['pairs'])} pairs ok"))
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

def ctx(need_api=True):
    cfg = load_config()
    conn = db_connect(cfg["db"])
    api = api_from_config(cfg) if need_api else None
    return cfg, conn, api


def sh(cmd, **kw):
    """subprocess.run that tolerates a missing binary (containers, tests)."""
    try:
        return subprocess.run(cmd, **kw)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(cmd, 127, "", "")


def reload_daemon():
    sh(["systemctl", "kill", "-s", "HUP", SERVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def op_add(master, inbound_id, mult, suffix="_tun", sub_link=None, assume_yes=False, quiet=False):
    """Create the shadow for `master` on `inbound_id`. Saga with compensation: every step that
    succeeded is undone if a later one fails, so a failure never leaves a half-configured user."""
    cfg, conn, api = ctx()
    sub_link = cfg.get("sub_link", True) if sub_link is None else sub_link
    kfp = mult_fp(parse_mult(mult))
    shadow = master + suffix
    say = (lambda *_: None) if quiet else info

    m = client_row(conn, master)
    if not m:
        raise XMError(f"client '{master}' not found")
    if find_pairs(cfg, shadow) or any(p["master"] == master and p["inbound_id"] == inbound_id for p in cfg["pairs"]):
        raise XMError(f"{master} is already paired on inbound {inbound_id}")
    if any(p["shadow"] == master for p in cfg["pairs"]):
        raise XMError(f"{master} is itself a shadow client")
    if client_row(conn, shadow):
        raise XMError(f"a client named '{shadow}' already exists — remove it or use another --suffix")
    ib = inbound_row(conn, inbound_id)
    if not ib:
        raise XMError(f"inbound {inbound_id} not found")
    if ib["protocol"] in ("wireguard", "amneziawg"):
        raise XMError("WireGuard/AmneziaWG inbounds are not supported")
    attached = inbound_id in client_inbound_ids(conn, m["id"])

    if not quiet:
        print(C.bold(f"\nAdd tunnel billing for {master}"))
        print(f"  tunnel inbound : #{ib['id']} {ib['remark'] or ''} ({ib['protocol']}:{ib['port']})")
        print(f"  shadow client  : {shadow} (same UUID/password)")
        print(f"  multiplier     : x{kfp / SCALE:g}")
        print(f"  fail-safe quota: {human(failsafe_total(m['total_gb'], kfp)) if m['total_gb'] else 'unlimited'}"
              f"   expiry: {fmt_expiry(m['expiry_time'])}")
        if attached:
            warn(f"{master} is attached to this inbound now; it will be moved to the shadow "
                 "(the tunnel reconnects once, a few seconds)")
        if not assume_yes and not confirm("Proceed?"):
            raise XMError("cancelled")

    rec = api.get_client(master)["client"]
    undo = []
    try:
        if attached:
            api.detach(master, [inbound_id])
            undo.append(("re-attach master", lambda: api.attach(master, [inbound_id])))
            say("detached master from the tunnel inbound")

        client = record_to_client(rec)
        client.update({
            "email": shadow,
            "subId": uuid.uuid4().hex[:16],   # v3.8.5 requires a unique subId per client
            "totalGB": failsafe_total(m["total_gb"], kfp),
            "expiryTime": m["expiry_time"] or 0,
            "limitIp": m["limit_ip"] or 0,
            "enable": m["enabled"],
            "tgId": 0,
            "group": "",                       # group totals would count tunnel bytes twice (raw + billed)
            "reset": 0, "resetDay": 0, "resetMax": 0, "trafficReset": "never",  # renewals follow the master
            "comment": f"xui-mult shadow of {master} (x{kfp / SCALE:g}) - do not edit",
            "flow": "",                        # the panel applies the inbound's own flow policy
        })
        client.pop("limitHwid", None)
        api.add_client(client, [inbound_id])
        undo.append(("delete shadow", lambda: api.delete_client(shadow)))
        say(f"created {shadow}")

        s = client_row(conn, shadow)
        if not s:
            raise XMError("shadow was created but is not visible in the database")
        if creds_differ(m, s):
            raise XMError("panel assigned different credentials to the shadow — aborting")
        # Ledger at the shadow's current counters (0 for a fresh client): every tunnel byte is billed.
        ledger_init(conn, shadow, s["up"], s["down"], m["used"], s["expiry_time"] or 0)
        undo.append(("drop ledger", lambda: ledger_delete(conn, shadow)))

        with config_txn() as c2:
            if find_pairs(c2, shadow):
                raise XMError("pair appeared concurrently — aborting")
            c2["pairs"].append({"master": master, "shadow": shadow, "multiplier": kfp / SCALE,
                                "inbound_id": inbound_id})
    except BaseException as e:
        for name, fn in reversed(undo):
            try:
                fn()
                say(f"rolled back: {name}")
            except Exception as ue:  # noqa: BLE001
                bad(f"rollback step '{name}' failed: {ue} — fix manually in the panel")
        raise XMError(f"add failed: {e}") from e

    reload_daemon()
    if sub_link:
        try:
            n = sync_sub_link(api, master, shadow)
            say(f"added {n} tunnel link(s) to {master}'s subscription")
        except XMError as e:
            warn(f"pair is active, but the subscription link was not added: {e}")
    if not quiet:
        ok(f"{master} now pays x{kfp / SCALE:g} for traffic through inbound #{inbound_id}")
    return shadow


def sync_sub_link(api, master, shadow):
    """Put the shadow's share link(s) into the master's subscription as external links."""
    links = api.client_links(shadow)
    if not links:
        raise XMError("the panel produced no share link for the shadow")
    if any("127.0.0.1" in l or "localhost" in l for l in links):
        raise XMError("share link points to 127.0.0.1 — set the public host with `xui-mult setup` "
                      "or set the inbound's external proxy in the panel")
    return _set_marked_links(api, master, shadow, links)


def _set_marked_links(api, master, shadow, links):
    current = api.get_client(master).get("externalLinks") or []
    marker = LINK_REMARK_PREFIX + shadow
    keep = [{"kind": x.get("kind") or "link", "value": x.get("value", ""), "remark": x.get("remark", ""),
             "enable": x.get("enable", True), "expiryTime": x.get("expiryTime", 0),
             "namePrefix": x.get("namePrefix", "")}
            for x in current if (x.get("remark") or "") != marker]
    keep += [{"kind": "link", "value": l, "remark": marker, "enable": True, "expiryTime": 0, "namePrefix": ""}
             for l in links]
    api.set_external_links(master, keep)
    return len(links)


def op_remove(email, delete_shadow=False, reattach=False):
    cfg, conn, api = ctx()
    p = one_pair(cfg, email)
    master, shadow = p["master"], p["shadow"]
    kfp = mult_fp(p["multiplier"])
    account(conn, master, shadow, kfp)            # final billing
    with config_txn() as c2:
        c2["pairs"] = [x for x in c2["pairs"] if x["shadow"] != shadow]
    reload_daemon()
    info(f"stopped billing {shadow}")
    with contextlib.suppress(XMError):
        _set_marked_links(api, master, shadow, [])
        info("removed tunnel link from the master's subscription")
    if reattach and client_row(conn, master):
        try:
            api.attach(master, [p["inbound_id"]])
            info(f"re-attached {master} to inbound #{p['inbound_id']} (1:1 billing again)")
        except XMError as e:
            warn(f"could not re-attach master: {e}")
    if delete_shadow and client_row(conn, shadow):
        account(conn, master, shadow, kfp)        # bytes that arrived meanwhile
        api.delete_client(shadow)
        info(f"deleted {shadow}")
    elif client_row(conn, shadow):
        warn(f"{shadow} was kept and is no longer managed: it still works on the tunnel, billed 1:1, and no "
             "longer follows the master's disable/expiry. Delete or disable it in the panel if needed.")
    ledger_delete(conn, shadow)
    ok(f"removed pair {master} / {shadow}")


def op_set_mult(email, mult):
    k = parse_mult(mult)
    with config_txn() as cfg:
        p = one_pair(cfg, email)
        old = p["multiplier"]
        p["multiplier"] = k
    reload_daemon()
    ok(f"{p['shadow']}: x{old} -> x{k} (applies to traffic from now on; fail-safe quota updates within a tick)")


def op_add_all(from_inbound, to_inbound, mult, suffix="_tun", assume_yes=False):
    cfg, conn, _ = ctx()
    paired = {p["master"] for p in cfg["pairs"] if p["inbound_id"] == to_inbound} | {p["shadow"] for p in cfg["pairs"]}
    rows = conn.execute("""SELECT c.email FROM clients c JOIN client_inbounds ci ON ci.client_id = c.id
                           WHERE ci.inbound_id = ? ORDER BY c.email""", (from_inbound,)).fetchall()
    emails = [r[0] for r in rows if r[0] not in paired and not r[0].endswith(suffix)]
    if not emails:
        info("nothing to do: every client of that inbound is already paired")
        return
    print(f"{len(emails)} client(s) of inbound #{from_inbound} will get a shadow on #{to_inbound} at x{mult}:")
    print("  " + ", ".join(emails[:20]) + (" …" if len(emails) > 20 else ""))
    if not assume_yes and not confirm("Proceed?"):
        return
    okc, fails = 0, []
    for e in emails:
        try:
            op_add(e, to_inbound, mult, suffix, assume_yes=True, quiet=True)
            okc += 1
            print(C.green("  ✔ ") + e)
        except XMError as ex:
            fails.append((e, str(ex)))
            print(C.red("  ✖ ") + f"{e}: {ex}")
    ok(f"{okc} added") if okc else None
    if fails:
        warn(f"{len(fails)} failed (see above) — each failure was rolled back")


def op_list():
    cfg, conn, _ = ctx(need_api=False)
    if not cfg["pairs"]:
        info("no users configured yet — use 'Add a user to a tunnel'")
        return
    hdr = f"{'MASTER':<22} {'SHADOW':<26} {'K':>5} {'TUNNEL RAW':>11} {'BILLED':>11} {'USED/QUOTA':>21} STATE"
    print(C.bold(hdr))
    for p in cfg["pairs"]:
        m, s = client_row(conn, p["master"]), client_row(conn, p["shadow"])
        led = ledger_row(conn, p["shadow"]) or {}
        if not m or not s:
            print(f"{p['master']:<22} {p['shadow']:<26} {p['multiplier']:>5} " + C.red("missing client"))
            continue
        quota = f"{human(m['used'])}/{human(m['total_gb']) if m['total_gb'] else '∞'}"
        pct = f" {100 * m['used'] // m['total_gb']}%" if m["total_gb"] else ""
        state = C.green("active") if m["enabled"] and s["enabled"] else (
            C.yellow("disabled") if not m["enabled"] and not s["enabled"] else C.red("out of sync"))
        print(f"{p['master']:<22} {p['shadow']:<26} {p['multiplier']:>5} {human(led.get('raw_total')):>11} "
              f"{human(led.get('billed_total')):>11} {quota + pct:>21} {state}")
    print(C.dim("TUNNEL RAW / BILLED = totals counted by xui-mult since the pair was added"))


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
        ok(f"config {CONF_PATH}")
    except (XMError, ValueError, OSError) as e:
        bad(f"config: {e}")
        return 1
    active = service_active()
    check(active, "service running", "service is not running", "xui-mult start")
    st = read_status()
    if st:
        age = int(time.time()) - st["last_tick"]
        check(age < max(60, 5 * cfg["interval"]), f"last tick {age}s ago ({st['tick_ms']} ms)",
              f"last tick {age}s ago — daemon stuck?", "xui-mult logs")
        check(st["api_errors"] == 0, "panel API sync healthy",
              f"{st['api_errors']} failed syncs in a row: {st['api_error']}", "xui-mult setup")
    elif active:
        warn("no heartbeat yet")
    try:
        conn = db_connect(cfg["db"])
        jm = q1(conn, "PRAGMA journal_mode")[0]
        ok(f"database {cfg['db']} (ledger table present, journal_mode={jm}, busy_timeout={BUSY_TIMEOUT_MS} ms)")
    except (XMError, sqlite3.Error) as e:
        bad(str(e))
        return 1
    if st and st.get("db_busy"):
        warn("the last tick found the database locked by the panel (billing resumes automatically)")
    ver = (sh(["/usr/local/x-ui/x-ui", "-v"], capture_output=True, text=True, timeout=5,
              stdin=subprocess.DEVNULL).stdout or "").strip()
    if ver:
        (ok if PANEL_VERIFIED.lstrip("v") in ver else warn)(
            f"panel version: {ver}" + ("" if PANEL_VERIFIED.lstrip("v") in ver else
                                       f"  (not verified — tool was verified on {PANEL_VERIFIED})"))
    try:
        api = api_from_config(cfg)
        api.ping()
        ok(f"panel API reachable at {cfg['api_url']}" + (f" (Host: {api.api_host})" if api.api_host else "")
           + ", token can manage clients")
    except XMError as e:
        problems += 1
        bad(f"panel API: {e}")
    for p in cfg["pairs"]:
        m, s = client_row(conn, p["master"]), client_row(conn, p["shadow"])
        tag = f"{p['master']} / {p['shadow']}"
        if not m or not s:
            check(False, "", f"{tag}: {'master' if not m else 'shadow'} client missing",
                  f"xui-mult remove {p['shadow']}")
            continue
        errs = []
        if creds_differ(m, s):
            errs.append("credentials differ")
        if p["inbound_id"] not in client_inbound_ids(conn, s["id"]):
            errs.append(f"shadow not on inbound #{p['inbound_id']}")
        if p["inbound_id"] in client_inbound_ids(conn, m["id"]):
            errs.append("master is ALSO on the tunnel inbound (billed 1:1 there)")
        m_on = m["enabled"] and not (m["ct_total"] and m["used"] >= m["ct_total"])
        if m_on != s["enabled"]:
            errs.append("enable state differs (fixed within one tick if the service runs)")
        if not ledger_row(conn, p["shadow"]):
            errs.append("no ledger row yet")
        check(not errs, f"{tag} x{p['multiplier']}", f"{tag}: " + "; ".join(errs))
    print()
    (ok("all checks passed") if not problems else warn(f"{problems} problem(s)"))
    return 0 if not problems else 1


def op_dry_run():
    cfg, conn, _ = ctx(need_api=False)
    if not cfg["pairs"]:
        info("no pairs configured")
        return
    for p in cfg["pairs"]:
        r = account(conn, p["master"], p["shadow"], mult_fp(p["multiplier"]), dry_run=True)
        if r is None:
            bad(f"{p['shadow']}: client missing")
        else:
            print(f"  {p['shadow']:<28} pending raw {human(r[0]):>10}  -> would bill {human(r[1]):>10}")
    info("nothing was written")


def op_setup(non_interactive=None):
    with config_txn() as cfg:
        if non_interactive:
            cfg.update({k: v for k, v in non_interactive.items() if v is not None})
        else:
            print(C.bold("\nPanel connection setup"))
            db = ask("Path to x-ui.db", cfg["db"] or DEFAULT_DB)
            conn = db_connect(db)
            url_guess, domain = detect_panel_url(conn)
            conn.close()
            print(C.dim(f"  detected from panel settings: {url_guess}"))
            url = ask("Panel URL (with base path)", cfg["api_url"] or url_guess).rstrip("/")
            print(C.dim("  Create a token in the panel: Settings → API tokens → new token with FULL access."))
            tok = ask("API token", "(keep current)" if cfg["api_token"] else None)
            if tok == "(keep current)":
                tok = cfg["api_token"]
            if domain:
                print(C.dim(f"  The panel has a domain set ({domain}): API calls are sent with Host: {domain}, and "
                            "share links use it\n  unless the tunnel inbound has an External Proxy address."))
                lh = domain
            else:
                print(C.dim("  Public address clients use to reach your tunnel/panel domain (goes into share links)."))
                lh = ask("Public host for share links", cfg.get("link_host") or "")
            cfg.update({"db": db, "api_url": url, "api_token": tok, "link_host": lh, "api_host": "auto"})
        test = api_from_config(cfg)
        test.ping()
    ok("connected to panel API with a full-access token — settings saved")
    reload_daemon()


def op_inbounds():
    _, conn, _ = ctx(need_api=False)
    print(C.bold(f"{'ID':>4}  {'PROTOCOL':<12}{'PORT':>6}  {'CLIENTS':>7}  REMARK"))
    for ib in all_inbounds(conn):
        n = q1(conn, "SELECT COUNT(*) FROM client_inbounds WHERE inbound_id=?", (ib["id"],))[0]
        print(f"{ib['id']:>4}  {ib['protocol']:<12}{ib['port']:>6}  {n:>7}  {ib['remark'] or ''}"
              + ("" if ib["enable"] else C.dim("  (disabled)")))


def op_resync():
    cfg, conn, api = ctx()
    stats = {"missing": set()}
    reconcile(conn, cfg, api, stats)
    n = 0
    if cfg.get("sub_link", True):
        for p in cfg["pairs"]:
            try:
                sync_sub_link(api, p["master"], p["shadow"])
                n += 1
            except XMError as e:
                warn(f"{p['shadow']}: {e}")
    ok(f"state re-synced; subscription links refreshed for {n} user(s)")


def systemctl(action):
    r = sh(["systemctl", action, SERVICE])
    if r.returncode == 0:
        ok(f"service {action}ed" if not action.endswith("e") else f"service {action}d")
    return r.returncode


def op_logs(follow=False, lines=100):
    cmd = ["journalctl", "-u", SERVICE, "-n", str(lines), "--no-pager"]
    if follow:
        cmd = ["journalctl", "-u", SERVICE, "-f", "-n", str(lines)]
    with contextlib.suppress(KeyboardInterrupt):
        sh(cmd)


def op_uninstall(assume_yes=False):
    if not assume_yes and not confirm("Remove xui-mult (service + files)?", False):
        return
    with contextlib.suppress(XMError, ValueError, OSError):
        pairs = load_config()["pairs"]
        # Shadows left behind keep working on the tunnel with nobody mirroring the master's disable/expiry.
        if pairs and (assume_yes or confirm(f"Unwind all {len(pairs)} tunnel pair(s) first: final billing, "
                                            "re-attach masters to the tunnel inbound, delete _tun clients?", True)):
            for p in pairs:
                try:
                    op_remove(p["shadow"], delete_shadow=True, reattach=True)
                except (XMError, sqlite3.Error) as e:
                    bad(f"{p['shadow']}: {e} — clean it up in the panel")
    drop = assume_yes or confirm("Also drop the ledger table from x-ui.db?", False)
    if drop:
        with contextlib.suppress(Exception):
            cfg = load_config()
            conn = sqlite3.connect(cfg["db"], timeout=10)
            conn.execute("DROP TABLE IF EXISTS tunnel_multiplier_ledger")
            conn.commit()
            conn.close()
    sh(["systemctl", "disable", "--now", SERVICE], stderr=subprocess.DEVNULL)
    for path in ("/etc/systemd/system/xui-mult.service", "/usr/local/bin/xui-mult"):
        with contextlib.suppress(OSError):
            os.unlink(path)
    sh(["systemctl", "daemon-reload"], stderr=subprocess.DEVNULL)
    subprocess.run(["rm", "-rf", "/usr/local/xui-mult", RUN_DIR])
    warn(f"kept {CONF_DIR} (your settings). Delete it manually if you want: rm -rf {CONF_DIR}")
    ok("xui-mult removed.")


# =================================================================================== menu

def header():
    try:
        cfg = load_config()
        pairs = len(cfg["pairs"])
    except (XMError, ValueError, OSError):
        cfg, pairs = None, "?"
    active = service_active()
    st = read_status()
    tick = f"{int(time.time()) - st['last_tick']}s ago" if st else "—"
    svc = C.green("● running") if active else C.red("● stopped")
    api = ""
    if st and st.get("api_errors"):
        api = C.red(f"   API errors: {st['api_errors']}")
    elif cfg is not None and not cfg.get("api_token"):
        api = C.yellow("   API not configured")
    print()
    print(C.bold(" xui-mult ") + C.dim(f"v{VERSION}") + " — tunnel traffic multiplier for 3X-UI")
    print(f" Service: {svc}   Users: {pairs}   Last tick: {tick}{api}")
    print(" " + "─" * 58)


MENU = [
    ("1", "Setup / change panel API connection"),
    ("2", "Add a user to a tunnel"),
    ("3", "Add ALL users of an inbound to a tunnel"),
    ("4", "List users and billed tunnel usage"),
    ("5", "Change a multiplier"),
    ("6", "Remove a user from tunnel billing"),
    ("7", "Status & health check"),
    ("8", "Live logs"),
    ("9", "Start / Stop / Restart service"),
    ("10", "Dry-run (show pending billing, write nothing)"),
    ("11", "List inbounds"),
    ("12", "Re-sync all shadows & subscription links"),
    ("13", "Uninstall"),
    ("0", "Exit"),
]


def int_val(v):
    return int(v)


def menu():
    while True:
        header()
        for k, label in MENU:
            print(f"  {C.cyan(k.rjust(2))}) {label}")
        try:
            choice = input("\n Choose: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        try:
            if choice == "0":
                return 0
            elif choice == "1":
                op_setup()
            elif choice == "2":
                op_inbounds()
                email = ask("Master client email")
                ib = ask("Tunnel inbound ID", validate=int_val)
                k = ask("Multiplier", "1.2", validate=parse_mult)
                op_add(email, ib, k)
            elif choice == "3":
                op_inbounds()
                src = ask("Take clients FROM inbound ID", validate=int_val)
                dst = ask("Tunnel inbound ID", validate=int_val)
                k = ask("Multiplier", "1.2", validate=parse_mult)
                op_add_all(src, dst, k)
            elif choice == "4":
                op_list()
            elif choice == "5":
                op_list()
                op_set_mult(ask("Master or shadow email"), ask("New multiplier", validate=parse_mult))
            elif choice == "6":
                op_list()
                email = ask("Master or shadow email")
                reatt = confirm("Re-attach the master to the tunnel inbound (1:1 billing)?", True)
                dels = confirm("Delete the _tun shadow client from the panel?", True)
                op_remove(email, delete_shadow=dels, reattach=reatt)
            elif choice == "7":
                op_status()
            elif choice == "8":
                print(C.dim("Ctrl+C to go back"))
                op_logs(follow=True, lines=50)
            elif choice == "9":
                a = ask("1) start  2) stop  3) restart", "3")
                systemctl({"1": "start", "2": "stop", "3": "restart"}.get(a, "restart"))
            elif choice == "10":
                op_dry_run()
            elif choice == "11":
                op_inbounds()
            elif choice == "12":
                op_resync()
            elif choice == "13":
                op_uninstall()
                return 0
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

def build_parser():
    ap = argparse.ArgumentParser(prog="xui-mult", description="Per-inbound traffic multiplier for 3X-UI "
                                 f"(verified on {PANEL_VERIFIED}). Run without arguments for the menu.")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--version", action="version", version=f"xui-mult {VERSION}")
    sp = ap.add_subparsers(dest="cmd")

    s = sp.add_parser("setup", help="configure panel API connection")
    s.add_argument("--url"); s.add_argument("--token"); s.add_argument("--db"); s.add_argument("--link-host")
    s.add_argument("--api-host", help="Host header for API calls ('auto' = the panel's domain setting)")

    s = sp.add_parser("add", help="bill a user's tunnel traffic with a multiplier")
    s.add_argument("email"); s.add_argument("--inbound", type=int, required=True)
    s.add_argument("--mult", type=parse_mult, required=True); s.add_argument("--suffix", default="_tun")
    s.add_argument("--no-sub-link", action="store_true"); s.add_argument("-y", "--yes", action="store_true")

    s = sp.add_parser("add-all", help="pair every client of one inbound with a tunnel inbound")
    s.add_argument("--from", dest="src", type=int, required=True); s.add_argument("--to", type=int, required=True)
    s.add_argument("--mult", type=parse_mult, required=True); s.add_argument("--suffix", default="_tun")
    s.add_argument("-y", "--yes", action="store_true")

    sp.add_parser("list", help="users and billed tunnel usage")
    s = sp.add_parser("set-mult", help="change a multiplier"); s.add_argument("email"); s.add_argument("mult")
    s = sp.add_parser("remove", help="stop tunnel billing for a user"); s.add_argument("email")
    s.add_argument("--delete-shadow", action="store_true"); s.add_argument("--reattach", action="store_true")
    sp.add_parser("status", help="health check (exit code 1 on problems)")
    s = sp.add_parser("logs", help="service logs"); s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-n", type=int, default=100)
    for a in ("start", "stop", "restart"):
        sp.add_parser(a, help=f"{a} the service")
    sp.add_parser("dry-run", help="show pending billing without writing")
    sp.add_parser("inbounds", help="list inbounds")
    sp.add_parser("resync", help="force state + subscription link sync")
    s = sp.add_parser("uninstall", help="remove xui-mult"); s.add_argument("-y", "--yes", action="store_true")
    s = sp.add_parser("run", help="daemon loop (used by systemd)")
    s.add_argument("--dry-run", action="store_true"); s.add_argument("--once", action="store_true")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.no_color:
        C.on = False
    if args.cmd == "run":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    else:
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
    if args.cmd not in (None, "list", "status", "logs", "dry-run", "inbounds") and os.geteuid() != 0:
        bad("run as root (sudo xui-mult …)")
        return 1
    try:
        if args.cmd is None:
            return menu()
        if args.cmd == "run":
            run_daemon(args)
        elif args.cmd == "setup":
            ni = {"api_url": args.url and args.url.rstrip("/"), "api_token": args.token, "db": args.db,
                  "link_host": args.link_host, "api_host": args.api_host}
            op_setup(ni if any(v for v in ni.values()) else None)
        elif args.cmd == "add":
            op_add(args.email, args.inbound, args.mult, args.suffix, sub_link=False if args.no_sub_link else None,
                   assume_yes=args.yes)
        elif args.cmd == "add-all":
            op_add_all(args.src, args.to, args.mult, args.suffix, args.yes)
        elif args.cmd == "list":
            op_list()
        elif args.cmd == "set-mult":
            op_set_mult(args.email, args.mult)
        elif args.cmd == "remove":
            op_remove(args.email, args.delete_shadow, args.reattach)
        elif args.cmd == "status":
            return op_status()
        elif args.cmd == "logs":
            op_logs(args.follow, args.n)
        elif args.cmd in ("start", "stop", "restart"):
            return systemctl(args.cmd)
        elif args.cmd == "dry-run":
            op_dry_run()
        elif args.cmd == "inbounds":
            op_inbounds()
        elif args.cmd == "resync":
            op_resync()
        elif args.cmd == "uninstall":
            op_uninstall(args.yes)
    except KeyboardInterrupt:
        print()
        return 130
    except (XMError, sqlite3.Error) as e:
        bad(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
