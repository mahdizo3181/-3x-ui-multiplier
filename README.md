```text
 __  __ _   _ ___       __  __ _   _ _   _____ ___ ____  _     ___ _____ ____
 \ \/ /| | | |_ _|     |  \/  | | | | | |_   _|_ _|  _ \| |   |_ _| ____|  _ \
  \  / | | | || | _____| |\/| | | | | |   | |  | || |_) | |    | ||  _| | |_) |
  /  \ | |_| || ||_____| |  | | |_| | |___| |  | ||  __/| |___ | || |___|  _ <
 /_/\_\ \___/|___|     |_|  |_|\___/|_____|_| |___|_|   |_____|___|_____|_| \_\
```

# 3X-UI Traffic Multiplier (xui-mult)

Daemon & CLI tool for inbound traffic multipliers and tunnel overhead compensation on 3X-UI (Sanaei) v3.8.5.

[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB.svg)](https://www.python.org/)
[![3X-UI v3.8.5](https://img.shields.io/badge/3X--UI-v3.8.5-2ea44f.svg)](https://github.com/MHSanaei/3x-ui/releases/tag/v3.8.5)
![Version v2.1.0](https://img.shields.io/badge/version-v2.1.0-informational.svg)
[![Tests 30/30 passing](https://img.shields.io/badge/tests-30%2F30%20passing-brightgreen.svg)](#verification--tests)

**English** · [فارسی](#فارسی)

---

## The problem

On a tunnel setup, the server moves more bytes than the user does. Transport and TLS framing,
retransmissions and the extra relay hop add up: a user who consumes **1 GB** can cost **1.2–1.3 GB** of
real server traffic.

3X-UI counts only the bytes each client sends and receives. It has no per-inbound traffic coefficient,
so tunnel users are billed 1:1 and you pay the overhead.

**xui-mult** lets you give an inbound a multiplier, like Marzban or Pasargad do for nodes:

```bash
xui-mult set 2 1.2     # every client on inbound #2 now pays 1.2x their traffic
```

That's the whole setup. Clients already on the inbound, and any you add later, are billed
automatically. Nothing is mapped per user.

| k = 1.2 | Traffic used | Deducted from quota |
|---|---|---|
| Client on a direct inbound | 1 GB | 1 GB |
| Client on the tunnel inbound | 1 GB | 1.2 GB |

## How it works

```mermaid
flowchart LR
    I["Inbound #2<br/>Germany Tunnel<br/>multiplier 1.2"] -- "clients attached<br/>(client_inbounds)" --> X["xui-mult<br/>every 7 s"]
    T[("client_traffics<br/>up / down")] -- "new traffic Δ" --> X
    X -- "+ ⌊Δ × (k − 1)⌋<br/>same row, atomic" --> T
    T -- "quota · expiry" --> P["3X-UI's own<br/>enforcement"]
```

1. **Membership.** Each tick, the daemon reads which clients are attached to each multiplied inbound
   from `client_inbounds`, the table 3X-UI v3.8.5 treats as authoritative. It does not use
   `client_traffics.inbound_id`: in v3.8.5 that column is a legacy pointer to the client's *first*
   inbound and goes stale. New clients are picked up on the next tick.
2. **Billing.** For each of those clients:

   ```text
   Δ      = current_total − last_synced_total      (per direction, up and down)
   extra  = ⌊Δ × (k − 1)⌋                          (fractions of a byte carry over)
   ```

   `extra` is added to the client's **own** traffic row. The panel then shows the multiplied usage,
   and its normal quota and expiry checks cut the client off when the quota is reached. xui-mult
   never disables anyone itself and never calls the panel API.
3. **Atomic, with a high-water-mark ledger.** Each tick is **one `BEGIN IMMEDIATE` SQLite
   transaction**:
   - it uses the same `up = MIN(up + ?, max)` statement as the panel;
   - it advances a ledger that stores each client's counters *after* the credit, so xui-mult never
     bills its own extra twice;
   - a crash rolls everything back, and the next tick bills the same bytes exactly once.

   3X-UI v3.8.5 opens SQLite with `_txlock=immediate` and adds traffic atomically. So the panel and
   xui-mult take turns on the write lock and never overwrite each other. This was checked in the
   v3.8.5 source and tested against a real panel.

   The daemon writes directly to the database instead of calling the panel API. The API's traffic
   endpoint sets absolute values, which would race with the panel's own traffic updates.

### One traffic counter per client
3X-UI keeps **one** traffic counter per client, shared by every inbound the client is on. So:

- A client attached to a multiplied inbound pays **k× on all of its traffic**. That includes traffic
  through a 1.0× inbound it is also attached to, because 3X-UI cannot tell the two apart.
  `xui-mult list` counts these clients for each inbound.
- A client on several multiplied inbounds pays the **highest** multiplier.

For exact per-inbound billing, keep tunnel users on tunnel inbounds only.

## Features
- **Inbound-centric:** the multiplier belongs to the inbound; clients are discovered automatically.
- **Minimal menu:** run `xui-mult`, like the native `x-ui` script. Four options on a dashboard card that
  shows the service state, the multiplied inbounds and the extra billed.
- **Aligned tables:** columns are measured in terminal cells, not characters, so flag emoji (🇩🇪),
  Persian remarks (including the zero-width نیم‌فاصله) and CJK text stay aligned. On narrow screens,
  like phone SSH apps, long columns are shortened with … instead of wrapping.
- **Five scriptable subcommands:** `set`, `list`, `remove`, `status`, `logs`. `status` exits with
  code 1 on problems, for monitoring.
- **No panel changes, no API token:** it adds one table (`xui_mult_ledger`) to `x-ui.db`. That's all.
- **Resilient systemd service:**
  - systemd's watchdog restarts it if it hangs, and it restarts automatically on failure;
  - only one copy runs at a time;
  - settings are reloaded without a restart;
  - it reconnects after a database restore;
  - if the panel holds the database lock for more than 10 s, the tick is skipped and billed in
    full next time.
- **Safe by design:**
  - traffic from before a multiplier is set is never billed;
  - removing a multiplier, or detaching a client, never bills the time in between when it comes back;
  - a client with corrupt counters is skipped without stopping the others.
- **Single file, standard library only:** Python 3.9+, no pip packages.

## Requirements
- Linux with systemd, root access.
- 3X-UI **v3.8.5** on **SQLite** (PostgreSQL panels are refused with a clear message).
- Python 3.9+. The installer installs `python3` with apt, dnf or yum if it is missing.

## Quick installation
Run both steps as root on the panel server.

**Step 1: Back up your current database (recommended)**

```bash
cp /etc/x-ui/x-ui.db /root/x-ui.db.bak
```

**Step 2: Run the installer**

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mahdizo3181/-3x-ui-multiplier/main/dist/install.sh)
```

When it finishes, the installer opens the menu. Choose **1**, pick the tunnel inbound and enter the
multiplier. Or do the same from the shell:

```bash
xui-mult set 2 1.2
xui-mult logs -f
```

Running the installer again upgrades in place and keeps your settings. For unattended installs
(Ansible, CI) the menu is skipped automatically; `XUI_MULT_NO_MENU=1` skips it anywhere.

## CLI usage

Running `xui-mult` with no arguments opens the dashboard:

```text
╭─ xui-mult v2.1.0 ────────────────────────────────────────────────────────╮
│ Inbound traffic multipliers for 3X-UI                                    │
├──────────────────────────────────────────────────────────────────────────┤
│ Service      ● running              Last tick    27s ago                 │
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

`xui-mult list` (multiplied inbounds are highlighted, the others greyed out, disabled ones get a red `[DISABLED]` badge):

```text
╭────┬────────────────┬───────────────┬─────────┬─────────┬──────────────╮
│ ID │ REMARK         │ PROTOCOL:PORT │ CLIENTS │    MULT │ EXTRA BILLED │
├────┼────────────────┼───────────────┼─────────┼─────────┼──────────────┤
│  1 │ Direct         │ vless:443     │   43/43 │   1.00x │            — │
│  2 │ Germany Tunnel │ vless:8443    │   14/15 │ [1.20x] │     +2.46 GB │
│  3 │ Tunnel 2       │ trojan:2083   │     6/6 │ [1.30x] │     +1.23 GB │
╰────┴────────────────┴───────────────┴─────────┴─────────┴──────────────╯
CLIENTS = active/total · EXTRA BILLED = added by xui-mult since the multiplier was set
! inbound #2: 3 client(s) are also on a lower-multiplier inbound. 3X-UI keeps one traffic counter
  per client, so ALL their traffic is billed 1.20x.
```

Each tick with traffic logs one line per inbound:

```text
INFO inbound #2 Germany Tunnel 1.20x: 14 client(s) used 12.30 GB -> +2.46 GB extra
```

Settings live in `/etc/xui-mult/config.json` (for example `"inbounds": {"2": 1.2, "3": 1.3}`).

## Verification & tests

| Suite | Runs against | Result |
|---|---|---|
| `tests/test_xui_mult.py` | A stand-in for the v3.8.5 database with the panel's own write patterns | **30 / 30 pass** on Python 3.9 and 3.14 |
| `tests/e2e_real_panel.py` | A real 3X-UI v3.8.5 binary built from the official source | **13 / 13 checks pass** |

The unit tests cover:
- exact ×k billing and fraction carry-over;
- membership through `client_inbounds` (not the stale pointer), and the highest multiplier winning;
- new clients, and history that is never billed;
- panel resets, multiplier changes, and remove / detach gaps;
- races with panel-style writers;
- a process killed mid-transaction, and interrupted ticks;
- idle ticks that write nothing;
- corrupt, negative or int64-overflowing counters;
- a locked or replaced database;
- the CLI, the menu and its input checks;
- table and card alignment with emoji, Persian and CJK text, in colour and plain, down to 60 columns.

Three bugs deliberately planted in the code were each caught by the tests.

The end-to-end run creates inbounds and clients through the real panel's API and bills simulated
traffic. It then runs the panel's own depletion pass to confirm that **3X-UI itself disables a
client once their ×1.2 usage reaches the quota**.

Run everything locally before deploying:

```bash
bash preflight.sh                                  # syntax, unit tests, build, installer == tested code
scp root@SERVER:/etc/x-ui/x-ui.db ./x-ui.db
bash preflight.sh --db ./x-ui.db --real-panel      # + a copy of your DB + e2e on a real v3.8.5 panel
```

- `--db` checks a **copy** of your production database; your file is never modified.
- `--real-panel` builds 3X-UI v3.8.5 from source (needs git, go and gcc, about 2 minutes the first time).

## Known limits
- **One counter per client:** see [above](#one-traffic-counter-per-client).
- Extra usage lands up to one tick (7 s) after the traffic, and the panel checks quotas every 5 s. A
  client can go slightly past their quota before being cut off, as with the panel's own checks.
- A brand-new client's traffic before its first tick (at most 7 s) is billed at 1×.
- If an admin raises a client's usage by hand in the panel, xui-mult sees the increase as traffic
  and multiplies it.
- At a reset or renewal, the last few seconds of traffic may go unbilled (in the user's favour).
- Inbounds on remote 3X-UI nodes have not been tested.
- Verified on v3.8.5 only. The service checks the database layout at start-up, and `status` warns on
  other panel versions. Re-test with `preflight.sh --real-panel` before upgrading 3X-UI.

## Troubleshooting

| `xui-mult status` says | Fix |
|---|---|
| service is not running | `systemctl start xui-mult`, then `xui-mult logs` |
| Inbound #N … no longer exists | The inbound was deleted in the panel: `xui-mult remove N` |
| not billed, unreadable counters: … | Those clients have corrupt usage values; fix or reset them in the panel |
| database locked by the panel | Nothing to do; the tick is billed in full on the next one |

## Upgrading from 1.x
1.x used per-user `_tun` shadow clients. 2.0 doesn't use them, and the installer warns if your config
still lists them:

1. Delete the `*_tun` clients in the panel.
2. Attach those users to the tunnel inbound directly.
3. Run `xui-mult set <tunnel inbound> <k>`.

## Uninstall
```bash
bash <(curl -Ls https://raw.githubusercontent.com/mahdizo3181/-3x-ui-multiplier/main/dist/install.sh) uninstall
```

This stops and removes the service and drops the `xui_mult_ledger` table. Clients and the usage
already billed are not touched. Settings stay in `/etc/xui-mult` until you delete them.

## Development
- `xui_mult.py` is the whole tool: daemon, CLI and menu.
- `install.sh.in` is the installer template. `bash build.sh` embeds `xui_mult.py` into
  `dist/install.sh`.
- Commit `dist/install.sh`: the one-line installer downloads it.

```bash
python3 -m unittest discover tests            # unit tests
python3 tests/e2e_real_panel.py /path/to/x-ui # end-to-end against a real panel binary
bash preflight.sh                             # all checks + build
```

## License
[GPL-3.0](LICENSE)

---

## فارسی

<div dir="rtl">

**xui-mult** سرویس و ابزار خط فرمانی برای پنل 3X-UI (نسخه‌ی v3.8.5 از Sanaei) است که به هر اینباند یک ضریب ترافیک می‌دهد. مثلاً با ضریب ۱٫۲، هر کلاینتِ آن اینباند به ازای هر ۱ گیگابایت مصرف، ۱٫۲ گیگابایت از حجمش کم می‌شود. به این ترتیب هزینه‌ی سربار تانل را کاربر می‌پردازد، نه شما.

### مشکل
در سرورهای تانل، سرور بیشتر از کاربر ترافیک جابه‌جا می‌کند. هدرهای انتقال و TLS، ارسال دوباره‌ی بسته‌ها و مسیر اضافه‌ی سرور واسط باعث می‌شوند هر ۱ گیگابایت مصرف کاربر، ۱٫۲ تا ۱٫۳ گیگابایت ترافیک واقعی روی سرور ایجاد کند. 3X-UI فقط بایت‌های خود کاربر را می‌شمارد و برای اینباندها ضریب ترافیک ندارد.

### روش کار
- ضریب به **اینباند** داده می‌شود، نه به کاربر. همه‌ی کلاینت‌های آن اینباند، و هر کلاینتی که بعداً اضافه شود، خودکار شامل می‌شوند.
- سرویس هر ۷ ثانیه ترافیک تازه‌ی هر کلاینت (Δ) را می‌خواند و ⌊Δ × (k − 1)⌋ را در یک تراکنش اتمیک SQLite به مصرف همان کلاینت اضافه می‌کند. یک دفتر حساب «آخرین مقدار دیده‌شده» را نگه می‌دارد تا هیچ بایتی دو بار حساب نشود.
- قطع کردن کاربر هنگام تمام شدن حجم یا انقضا را خود پنل انجام می‌دهد. xui-mult به API پنل و توکن نیازی ندارد.
- ترافیکِ قبل از تعیین ضریب هرگز حساب نمی‌شود.

### نکته‌ی مهم
3X-UI برای هر کلاینت فقط **یک** شمارنده‌ی ترافیک دارد. اگر کلاینتی هم روی اینباند مستقیم و هم روی اینباند تانل باشد، **تمام** ترافیکش با ضریب حساب می‌شود، چون پنل نمی‌تواند این دو را از هم جدا کند. دستور `xui-mult list` تعداد این کلاینت‌ها را نشان می‌دهد. برای محاسبه‌ی دقیق، کاربران تانل را فقط روی اینباند تانل بگذارید.

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

پس از نصب، منوی برنامه خودکار باز می‌شود: گزینه‌ی ۱ را بزنید، اینباند تانل را انتخاب کنید و ضریب را وارد کنید.

پیش‌نیازها: لینوکس با systemd، پایتون ۳٫۹ یا بالاتر (اگر نصب نباشد، خودکار نصب می‌شود) و 3X-UI روی SQLite. پنل‌هایی که PostgreSQL دارند پشتیبانی نمی‌شوند. اجرای دوباره‌ی نصب‌کننده برنامه را به‌روز می‌کند و تنظیمات حفظ می‌شوند.

### دستورات

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

### تست‌ها
- **۳۰ تست خودکار:** محاسبه‌ی دقیق ضریب، کلاینت‌های جدید، ریست و تمدید، رقابت هم‌زمان با پنل، قطع شدن برنامه وسط تراکنش، دیتابیس قفل‌شده، سرریز عدد، و هم‌ترازی جدول‌ها با ایموجی و متن فارسی.
- **۱۳ بررسی سرتاسری** روی باینری واقعی 3X-UI v3.8.5 که از سورس رسمی ساخته شده است. این بررسی‌ها نشان می‌دهند که خود پنل، کلاینت را وقتی مصرفِ ضریب‌دارش به سقف حجم برسد غیرفعال می‌کند.

پیش از استقرار، روی کامپیوتر خودتان `bash preflight.sh` را اجرا کنید.

### حذف
دستور نصب را با `uninstall` در انتها اجرا کنید. سرویس و جدول دفتر حساب حذف می‌شوند، ولی کلاینت‌ها و مصرفِ ثبت‌شده دست نمی‌خورند.

### مجوز
[GPL-3.0](LICENSE)

</div>
