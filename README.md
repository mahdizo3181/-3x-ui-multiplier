```text
 __  __ _   _ ___       __  __ _   _ _   _____ ___ ____  _     ___ _____ ____
 \ \/ /| | | |_ _|     |  \/  | | | | | |_   _|_ _|  _ \| |   |_ _| ____|  _ \
  \  / | | | || | _____| |\/| | | | | |   | |  | || |_) | |    | ||  _| | |_) |
  /  \ | |_| || ||_____| |  | | |_| | |___| |  | ||  __/| |___ | || |___|  _ <
 /_/\_\ \___/|___|     |_|  |_|\___/|_____|_| |___|_|   |_____|___|_____|_| \_\
```

# 3X-UI Traffic Multiplier (xui-mult)

[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB.svg)](https://www.python.org/)
[![3X-UI v3.8.5](https://img.shields.io/badge/3X--UI-v3.8.5-2ea44f.svg)](https://github.com/MHSanaei/3x-ui/releases/tag/v3.8.5)
![Version v2.2.0](https://img.shields.io/badge/version-v2.2.0-informational.svg)
[![SQLite + PostgreSQL](https://img.shields.io/badge/database-SQLite%20%2B%20PostgreSQL-336791.svg)](#postgresql)
[![Tests 99/99 passing](https://img.shields.io/badge/tests-99%2F99%20passing-brightgreen.svg)](tests/)

**English** · [فارسی](#فارسی)

## Overview

`xui-mult` is a lightweight daemon and CLI that assigns traffic multipliers directly to 3X-UI inbounds.
Set `1.2` on a tunnel inbound and every client on it, including clients added later, pays 1.2× their
traffic. There is no per-user setup. It works with both panel databases, **SQLite and PostgreSQL**, and a
tick stays in the millisecond range with hundreds of clients per inbound.

## Quick installation

Run as root on the panel server.

**Step 1: Back up your current database (recommended)**

```bash
cp /etc/x-ui/x-ui.db /root/x-ui.db.bak                       # SQLite panel
pg_dump "$XUI_DB_DSN" > /root/x-ui.pg.bak                     # PostgreSQL panel (DSN: see below)
```

**Step 2: Run the installer**

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mahdizo3181/-3x-ui-multiplier/main/dist/install.sh)
```

The installer detects whether the panel uses SQLite or PostgreSQL. For PostgreSQL it also installs the
Python driver (`python3-psycopg2`) and checks the connection before starting anything. The menu opens when
it finishes. Running the installer again upgrades in place and keeps your settings.

## Usage

Run `xui-mult` to open the dashboard:

```text
╭─ xui-mult v2.1.0 ────────────────────────────────────────────────────────╮
│ Inbound traffic multipliers for 3X-UI                                    │
├──────────────────────────────────────────────────────────────────────────┤
│ Service      ● running              Last tick    3s ago                  │
│ Multiplied   2 inbound(s)           Extra billed +2.46 GB since start    │
│ Database     PostgreSQL · 12 ms/tick · 917 clients                       │
│ Inbounds     #2 1.20x · #3 1.30x                                         │
├──────────────────────────────────────────────────────────────────────────┤
│  1  Set / edit multiplier for an inbound                                 │
│  2  List inbounds & multipliers                                          │
│  3  Remove multiplier from an inbound                                    │
│  4  Service status & logs                                                │
│  0  Exit                                                                 │
╰──────────────────────────────────────────────────────────────────────────╯
```

| Command | What it does |
|---|---|
| `xui-mult set <inbound_id> <multiplier>` | Set or change an inbound's multiplier (above 1.0, up to 10.0) |
| `xui-mult list` | Inbounds with remark, port, active/total clients, multiplier and extra billed |
| `xui-mult remove <inbound_id>` | Put the inbound back to 1.0× (usage already billed stays) |
| `xui-mult status` | Health check; exit code 1 on problems |
| `xui-mult db` | Which database is used (and a connection test); `db set <file or DSN>` pins one, `db auto` follows the panel |
| `xui-mult logs [-f] [-n N]` | Service logs, including every tick's billing |

```bash
xui-mult set 2 1.2       # inbound #2 pays 1.2x from now on
xui-mult set 3 1.35      # another tunnel inbound
xui-mult list            # what is multiplied and how much extra was billed
xui-mult remove 3        # back to 1.0x
xui-mult status          # health check
xui-mult logs -f         # live billing
```

## PostgreSQL

Nothing to configure. 3X-UI stores its database settings in its service's env file (`/etc/default/x-ui` on
Debian/Ubuntu, `/etc/sysconfig/x-ui` on RHEL, `/etc/conf.d/x-ui` on Arch):

```ini
XUI_DB_TYPE=postgres
XUI_DB_DSN=postgres://xui:password@127.0.0.1:5432/xui?sslmode=disable
```

xui-mult reads the same file **on every tick**, so it connects with the panel's own credentials and follows
the panel if you migrate it from SQLite to PostgreSQL (`x-ui migrate-db`), with no restart. The DSN may be
a `postgres://` URL or `host=… user=… dbname=…` pairs. Its password is never printed or logged.

To use something else (a different host, a read-through account), pin it:

```bash
xui-mult db set 'postgres://user:pass@db.internal:5432/xui?sslmode=require'   # tested before it is saved
xui-mult db auto                                                              # follow the panel again
```

A pinned DSN is stored in `/etc/xui-mult/config.json` (mode 600, root only). `XUI_MULT_DB=<file or DSN>` in
the service's environment overrides both.

**Driver.** SQLite needs only the Python standard library. PostgreSQL needs `psycopg2` (or `psycopg` 3),
which the installer adds with `apt`, `dnf`, `yum` or `pacman` (`python3-psycopg2` / `python-psycopg2`).

**Safe next to the panel.** Billing rows are locked with `SELECT … FOR UPDATE SKIP LOCKED`. The panel's own
`up = up + ?` simply waits the few milliseconds we hold a row, while we never wait on a row the panel holds,
so there is no lock cycle and no deadlock whatever order the panel updates its rows in (a client the panel is
mid-write on is billed on the next tick). A PostgreSQL advisory lock makes the daemon and a CLI command take
turns; `lock_timeout` is 5 s and `idle_in_transaction_session_timeout` 60 s, so a stalled xui-mult can never
hold the panel's rows. The connection is re-opened by itself after a PostgreSQL restart.

## Performance

Measured with `python3 tests/bench.py` (3 inbounds, 2,000 clients on the direct one, the rest spread over two
multiplied tunnel inbounds; local SQLite and PostgreSQL 18, one core):

| database | multiplied clients | idle tick | 3% of clients active | all active | statements per tick | peak memory |
|---|---:|---:|---:|---:|---:|---:|
| SQLite | 917 | 1.9 ms | 4.0 ms | 6.8 ms | 10 | 1.4 MiB |
| SQLite | 4,584 | 10.4 ms | 22.0 ms | 34 ms | 10 | 6.7 MiB |
| PostgreSQL | 917 | 3.3 ms | 8.2 ms | 25 ms | 11 | 1.4 MiB |
| PostgreSQL | 4,584 | 15.4 ms | 35.6 ms | 128 ms | 11 | 6.7 MiB |

A 7-second tick is therefore a fraction of a percent of CPU. Three things keep it that way:

- **A fixed number of statements.** However many clients are billed, a tick is 10–11 statements: every
  write is one set-based statement over all the clients (`UPDATE … FROM unnest(…)` on PostgreSQL,
  one prepared `executemany` on SQLite).
- **An idle tick costs a read and nothing else.** It looks first without any lock; only if something needs
  billing does it open the transaction. An idle tick writes nothing (on PostgreSQL: no WAL), holds no lock
  and is the usual tick on a quiet panel.
- **Indexed lookups only.** It joins `client_inbounds(inbound_id)` → `clients(email)` →
  `client_traffics(email)`, all indexes 3X-UI creates, and reads only the multiplied inbounds' clients, not
  the whole table.

Batching pays most when the database is on another machine. Against one statement per client (the same plan
written naively):

| database | multiplied clients | this release | one statement per client |
|---|---:|---:|---:|
| PostgreSQL, local | 917 | 25 ms | 59 ms (1,843 statements) |
| PostgreSQL, local | 4,584 | 128 ms | 303 ms (9,177 statements) |
| PostgreSQL, 1 ms network latency | 917 | 37 ms | 2.1 s |
| PostgreSQL, 1 ms network latency | 4,584 | 133 ms | 10.4 s, which does not fit in the 7 s interval |

(`bench.py --rtt-ms 1` simulates the latency.) Against the previous release (v2.1) on SQLite, an idle tick costs
the same, and a tick that bills every client takes about 3.4 ms more per 1,000 clients (34 ms against
18 ms at 4,584): the price of looking first without a lock, so that the usual idle tick never blocks the
panel's writes.

## How it works

```mermaid
flowchart LR
    I["Inbound #2<br/>Germany Tunnel<br/>multiplier 1.2"] -- "clients attached<br/>(client_inbounds)" --> X["xui-mult<br/>every 7 s"]
    T[("client_traffics<br/>up / down")] -- "new traffic Δ" --> X
    X -- "+ ⌊Δ × (k − 1)⌋<br/>same row, atomic" --> T
    T -- "quota · expiry" --> P["3X-UI's own<br/>enforcement"]
```

- **Membership:** every 7 s the daemon reads each multiplied inbound's clients from `client_inbounds`,
  so new clients are picked up automatically.
- **Billing:** `extra = ⌊Δ × (k − 1)⌋`, where `Δ` is the traffic since the last tick. It is added to the
  client's own traffic row, and 3X-UI's normal quota and expiry checks do the rest. No panel API calls,
  no token.
- **Atomic:** each tick that has something to bill is one transaction (`BEGIN IMMEDIATE` on SQLite, row
  locks on PostgreSQL). It uses the panel's own `up = MIN(up + ?, max)` update and advances a high-water-mark
  ledger (`xui_mult_ledger`, in the panel's own database) in the same commit. It can't race the panel,
  bills nothing twice, and a crash rolls the whole tick back.
- **One counter per client:** 3X-UI keeps a single traffic counter per client across all its inbounds.
  A client on both a direct and a multiplied inbound therefore pays k× on **all** its traffic, and a
  client on two multiplied inbounds pays the higher one. `xui-mult list` flags these clients.

## Requirements

- Linux with systemd, root access.
- 3X-UI **v3.8.5** on **SQLite or PostgreSQL**.
- Python 3.9+ (the installer installs it if missing); for PostgreSQL also `python3-psycopg2` (the installer
  installs it).

## Testing

```bash
bash preflight.sh                  # unit + scale tests on SQLite and PostgreSQL, build, installer == tested source
bash preflight.sh --bench          # tick time / statements / memory against client count
bash preflight.sh --real-panel     # end to end against a real v3.8.5 panel, on SQLite and on PostgreSQL
bash preflight.sh --db <file|DSN>  # compatibility of your own database (SQLite: on a copy; PostgreSQL: read-only)
```

PostgreSQL tests start a throwaway local server (`initdb`, listening on 127.0.0.1 only) and skip, saying why,
if no server binaries or driver are available. They include 600+ clients per inbound, a panel writing rows in
random order while we bill (no deadlocks), two billers racing, a killed connection, a PostgreSQL restart,
and a migration from SQLite to PostgreSQL while the daemon runs.

## Uninstall

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mahdizo3181/-3x-ui-multiplier/main/dist/install.sh) uninstall
```

This removes the service and the ledger table (from SQLite or PostgreSQL, whichever the panel uses).
Clients and the usage already billed are not touched, and settings stay in `/etc/xui-mult`.

---

## فارسی

<div dir="rtl">

### معرفی
**xui-mult** یک سرویس و ابزار خط فرمان سبک برای پنل 3X-UI است که به اینباندها ضریب ترافیک می‌دهد. با ضریب ۱٫۲ روی اینباند تانل، همه‌ی کلاینت‌های آن اینباند، حتی کلاینت‌هایی که بعداً اضافه می‌شوند، ۱٫۲ برابر مصرفشان حساب می‌شوند. نیازی به تنظیم جداگانه برای هر کاربر نیست.

نکته: 3X-UI برای هر کلاینت فقط یک شمارنده‌ی ترافیک دارد. پس کلاینتی که هم روی اینباند مستقیم و هم روی اینباند تانل باشد، روی **کل** ترافیکش ضریب می‌خورد.

### نصب
هر دو گام را روی سرور پنل و با کاربر root اجرا کنید.

**گام ۱: پشتیبان‌گیری از دیتابیس فعلی (پیشنهادی)**

</div>

```bash
cp /etc/x-ui/x-ui.db /root/x-ui.db.bak
```

<div dir="rtl">

**گام ۲: اجرای نصب‌کننده**

</div>

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mahdizo3181/-3x-ui-multiplier/main/dist/install.sh)
```

<div dir="rtl">

پس از نصب، منوی برنامه خودکار باز می‌شود. پیش‌نیازها: لینوکس با systemd، پایتون ۳٫۹ یا بالاتر، و 3X-UI نسخه‌ی v3.8.5 روی SQLite **یا PostgreSQL**.

### پشتیبانی از PostgreSQL
نیازی به تنظیم نیست. نصب‌کننده نوع دیتابیس پنل را از فایل تنظیمات سرویس خودش (`/etc/default/x-ui` یا `/etc/sysconfig/x-ui` یا `/etc/conf.d/x-ui`، مقدارهای `XUI_DB_TYPE` و `XUI_DB_DSN`) تشخیص می‌دهد، درایور پایتون (`python3-psycopg2`) را نصب می‌کند و پیش از راه‌اندازی اتصال را آزمایش می‌کند. سرویس در هر دور همان فایل را دوباره می‌خواند؛ پس اگر پنل را از SQLite به PostgreSQL منتقل کنید (`x-ui migrate-db`) نیازی به راه‌اندازی مجدد نیست. رمز دیتابیس هرگز چاپ یا لاگ نمی‌شود. برای اتصال دلخواه: `xui-mult db set 'postgres://…'` و برای برگشت: `xui-mult db auto`.

ردیف‌های در حال محاسبه با `FOR UPDATE SKIP LOCKED` قفل می‌شوند؛ یعنی هیچ‌وقت منتظر قفل پنل نمی‌مانیم و بن‌بست (deadlock) با پنل ممکن نیست. هر دور محاسبه فقط چند دستور SQL گروهی است، نه یک دستور برای هر کلاینت: با ۴٬۵۸۴ کلاینت دارای ضریب، دور خلوت حدود ۱۰ تا ۱۵ میلی‌ثانیه و دور پرترافیک (همه‌ی کلاینت‌ها فعال) کمتر از ۱۵۰ میلی‌ثانیه طول می‌کشد.

### دستورات کاربردی

</div>

```bash
xui-mult                  # منوی عددی
xui-mult set 2 1.2        # ضریب ۱٫۲ برای اینباند شماره‌ی ۲
xui-mult list             # فهرست اینباندها، ضریب‌ها و تعداد کلاینت‌ها
xui-mult remove 2         # برگرداندن اینباند به ضریب ۱
xui-mult status           # بررسی سلامت سرویس
xui-mult db               # دیتابیس در حال استفاده (SQLite یا PostgreSQL) و آزمایش اتصال
xui-mult logs -f          # لاگ زنده‌ی محاسبه‌ها
```

<div dir="rtl">

### حذف

</div>

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mahdizo3181/-3x-ui-multiplier/main/dist/install.sh) uninstall
```

<div dir="rtl">

سرویس و جدول دفتر حساب حذف می‌شوند. کلاینت‌ها و مصرفِ ثبت‌شده دست نمی‌خورند.

</div>
