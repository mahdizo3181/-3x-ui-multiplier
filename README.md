# xui-mult — tunnel traffic multiplier for 3X-UI

Makes traffic through a tunnel inbound count **k × more** against a user's quota (for example 1.2×),
so tunnel overhead is billed to the user. Verified against **3X-UI v3.8.5** (MHSanaei).

## How it works (one paragraph)
For each user, xui-mult creates a copy of the user called `<email>_tun` on the tunnel inbound: same
UUID/password, so nothing changes for the user. Because it has its own email, the panel counts tunnel
traffic separately. A background service moves that traffic × k onto the real user every 7 seconds,
in one database transaction that cannot collide with the panel's own writes. The real user's quota,
expiry and disabled state are copied to the `_tun` client through the panel API; a user whose quota is
used up loses the tunnel in the same tick. The tunnel link is added to the real user's subscription,
so users keep **one subscription link**.

## Pre-flight (on your computer, before deploying)
```bash
bash preflight.sh                                   # syntax, 34 tests, build, installer == tested code
scp root@YOUR_SERVER:/etc/x-ui/x-ui.db ./x-ui.db    # optional: a copy of the production database
bash preflight.sh --db ./x-ui.db --real-panel       # + checks on that copy + end-to-end run on a real 3X-UI v3.8.5
```
`--real-panel` builds 3X-UI v3.8.5 from source (needs git, go and gcc, about 2 minutes the first time) and
runs xui-mult against it on 127.0.0.1 with a throwaway database. The `--db` check runs on a temporary
copy; your file is not modified. The installer is `dist/install.sh`.

## Install
Copy `dist/install.sh` to the server and run it as root:
```bash
scp dist/install.sh root@YOUR_SERVER:/root/
ssh root@YOUR_SERVER 'bash /root/install.sh'
```
If you put `install.sh` in a GitHub repo, the one-liner is:
```bash
bash <(curl -Ls https://raw.githubusercontent.com/YOU/xui-mult/main/install.sh)
```
Needs: Linux with systemd, python3 ≥ 3.9 (installed automatically on Debian/Ubuntu), and 3X-UI on SQLite.
Running the installer again upgrades in place and keeps your settings.

## First setup
1. **Back up the panel database first:** `cp /etc/x-ui/x-ui.db /root/x-ui.db.bak`
2. In the panel, create an **API token with full access** (Settings → API tokens). Setup refuses
   monitor and node-sync tokens: they cannot disable clients.
3. Run `xui-mult` and choose **1) Setup**. The panel URL is detected automatically. Enter the token
   and the **public host** your users connect to (it goes into the tunnel share links). If the panel has
   a domain set, API calls are sent with that domain as Host (the panel refuses any other) and share
   links use it, unless the tunnel inbound has an External Proxy address.
4. Choose **2) Add a user to a tunnel**, pick the tunnel inbound and the multiplier.
   Try it on **one test user first**, then run `xui-mult dry-run`, `xui-mult status` and
   `xui-mult logs -f` for a few minutes. Then use **3) Add ALL users of an inbound**.

## Daily use
Run `xui-mult` for the menu. The same actions are available as commands:
```
xui-mult add alice --inbound 5 --mult 1.2      xui-mult list
xui-mult add-all --from 1 --to 5 --mult 1.2    xui-mult status      (exit code 1 on problems)
xui-mult set-mult alice 1.3                    xui-mult logs -f
xui-mult remove alice --delete-shadow --reattach
xui-mult dry-run   xui-mult resync   xui-mult restart   xui-mult uninstall
```

## Rules to remember
- **Don't edit `_tun` clients by hand.** xui-mult manages them. To block a user, disable the
  **real** user; the `_tun` client follows within one tick.
- Change quota, expiry and IP limit on the real user only; they are copied to `_tun` automatically.
- When adding a user who is already on the tunnel inbound, the user is moved to the `_tun` copy.
  Their tunnel connection drops once, for a few seconds.
- Renewing or resetting the real user also resets the `_tun` counter automatically.
- A user who runs out of quota loses the tunnel at once, without waiting for the panel to disable them.
- If the real user is deleted or renamed, the `_tun` client is disabled (`xui-mult status` names it).
- "Start after first use" plans: if the user's first connection is through the tunnel, the real user's
  clock starts at that moment too.
- `xui-mult remove` without `--delete-shadow` leaves an unmanaged `_tun` client that no longer follows
  the real user. `xui-mult uninstall` offers to remove all pairs first.

## Safety design
- **Billing:** one `BEGIN IMMEDIATE` SQLite transaction per user per tick. It uses the same atomic
  `up = MIN(up + ?, max)` statement as the panel, plus a ledger table inside `x-ui.db`, so the credit
  and the bookkeeping commit together. The `_tun` row is never written. Fractions of a byte carry over,
  so billed = ⌊raw × k⌋ exactly.
- **Checked in the v3.8.5 source:**
  - the panel adds traffic atomically (`up = MIN(up + ?, max)`); it never re-saves stale counters;
  - every panel transaction takes the write lock first (`_txlock=immediate`, `busy_timeout=10000`, WAL);
  - the renewal job loads and saves its rows inside one such transaction;
  - a traffic reset loads the row before its transaction, but writes zeros, so the most it can drop is
    the credit of the last few seconds before a reset (in the user's favour).

  So the two writers can't overwrite each other, and because the `_tun` row is never written, nothing
  the panel does to it can make a byte count twice.
- **Database:** busy timeout 10 s like the panel; the journal mode is left as the panel set it. If the
  panel holds the lock longer, the tick is skipped as a whole and billed in full on the next one.
- **State changes:** everything except the traffic credit goes through the panel API, so Xray is
  updated live. The service compares desired and actual state every tick, so a failed call is retried
  and a crash loses nothing. Blocking the tunnel runs even when another sync step fails, a panel
  answer that skipped some clients counts as an error, and one broken user never stops the others.
- **Adding a user:** each step is undone if a later one fails. The ledger starts at 0 before billing
  begins, so no bytes go unbilled.
- **Fail-safe:** each `_tun` client has quota = real quota ÷ k and the same expiry. Even if xui-mult
  stops, the panel still caps tunnel use.
- **Service:** only one copy can run at a time; systemd's watchdog restarts it if it hangs; it
  reconnects if you restore the database from a backup.

## Known limits
- After a user runs out, the tunnel keeps working until the next xui-mult tick (7 s), or up to about one
  panel tick for a user disabled by hand. Connections that are already open may continue briefly.
- The `_tun` copy has its own IP-limit counter and online status.
- At a renewal, tunnel bytes from the last few seconds before the reset may go unbilled (in the user's favour).
- SQLite panels only; PostgreSQL is refused with a clear message.
- Newer panel versions: the tool checks the database layout at start-up, and `xui-mult status` warns
  when the panel version isn't the verified one. Re-check the release notes before upgrading 3X-UI.

## Troubleshooting
`xui-mult status` checks every part and prints a fix for anything wrong. `xui-mult logs -f` shows
what the service is doing. Settings are in `/etc/xui-mult/config.json`.

## Development
`python3 -m unittest discover tests` runs the tests against a mock of the v3.8.5 panel (same tables,
endpoints, token scopes, domain check and answers). `python3 tests/e2e_real_panel.py /path/to/x-ui`
runs the end-to-end checks against a real panel binary. `bash build.sh` rebuilds `dist/install.sh`;
`bash preflight.sh` does all of it.
