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
![Version v2.1.0](https://img.shields.io/badge/version-v2.1.0-informational.svg)
[![Tests 30/30 passing](https://img.shields.io/badge/tests-30%2F30%20passing-brightgreen.svg)](tests/)

**English** · [فارسی](#فارسی)

## Overview

`xui-mult` is a lightweight daemon and CLI that assigns traffic multipliers directly to 3X-UI inbounds.
Set `1.2` on a tunnel inbound and every client on it, including clients added later, pays 1.2× their
traffic. There is no per-user setup.

## Quick installation

Run as root on the panel server.

**Step 1: Back up your current database (recommended)**

```bash
cp /etc/x-ui/x-ui.db /root/x-ui.db.bak
```

**Step 2: Run the installer**

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mahdizo3181/-3x-ui-multiplier/main/dist/install.sh)
```

The menu opens when the installer finishes. Running the installer again upgrades in place and keeps
your settings.

## Usage

Run `xui-mult` to open the dashboard:

```text
╭─ xui-mult v2.1.0 ────────────────────────────────────────────────────────╮
│ Inbound traffic multipliers for 3X-UI                                    │
├──────────────────────────────────────────────────────────────────────────┤
│ Service      ● running              Last tick    3s ago                  │
│ Multiplied   2 inbound(s)           Extra billed +2.46 GB since start    │
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
| `xui-mult logs [-f] [-n N]` | Service logs, including every tick's billing |

```bash
xui-mult set 2 1.2       # inbound #2 pays 1.2x from now on
xui-mult set 3 1.35      # another tunnel inbound
xui-mult list            # what is multiplied and how much extra was billed
xui-mult remove 3        # back to 1.0x
xui-mult status          # health check
xui-mult logs -f         # live billing
```

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
- **Atomic:** each tick is one `BEGIN IMMEDIATE` SQLite transaction. It uses the panel's own
  `up = MIN(up + ?, max)` update and advances a high-water-mark ledger (`xui_mult_ledger` in `x-ui.db`)
  in the same commit. It can't race the panel, bills nothing twice, and a crash rolls the whole tick
  back.
- **One counter per client:** 3X-UI keeps a single traffic counter per client across all its inbounds.
  A client on both a direct and a multiplied inbound therefore pays k× on **all** its traffic, and a
  client on two multiplied inbounds pays the higher one. `xui-mult list` flags these clients.

## Requirements

- Linux with systemd, root access.
- 3X-UI **v3.8.5** on **SQLite** (PostgreSQL is not supported).
- Python 3.9+ (the installer installs it if missing).

## Uninstall

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mahdizo3181/-3x-ui-multiplier/main/dist/install.sh) uninstall
```

This removes the service and the ledger table. Clients and the usage already billed are not touched,
and settings stay in `/etc/xui-mult`.

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

پس از نصب، منوی برنامه خودکار باز می‌شود. پیش‌نیازها: لینوکس با systemd، پایتون ۳٫۹ یا بالاتر، و 3X-UI نسخه‌ی v3.8.5 روی SQLite.

### دستورات کاربردی

</div>

```bash
xui-mult                  # منوی عددی
xui-mult set 2 1.2        # ضریب ۱٫۲ برای اینباند شماره‌ی ۲
xui-mult list             # فهرست اینباندها، ضریب‌ها و تعداد کلاینت‌ها
xui-mult remove 2         # برگرداندن اینباند به ضریب ۱
xui-mult status           # بررسی سلامت سرویس
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
