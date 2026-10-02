#!/usr/bin/env bash
# Pre-flight checks before deploying xui-mult. Runs locally and only writes to dist/ and temp dirs.
#   bash preflight.sh                       syntax, unit + scale tests (SQLite, and PostgreSQL when a server is
#                                           installed), build, installer payload == tested source
#   bash preflight.sh --db ./x-ui.db        + compatibility check on a COPY of your production SQLite database
#   bash preflight.sh --db 'postgres://…'   + READ-ONLY compatibility check of a live PostgreSQL panel
#   bash preflight.sh --bench               + tick time / statements / memory against client count
#   bash preflight.sh --real-panel [X-UI]   + end-to-end run against a real 3X-UI binary, on SQLite and (if a
#                                             PostgreSQL server is installed) PostgreSQL; without a path, v3.8.5
#                                             is built from source (needs git, go and gcc)
# PostgreSQL tests use a throwaway local server (initdb) and need a Python driver: if python3 has none, a
# private venv with psycopg2-binary is created in the cache dir (needs network once; --no-pg skips all of it).
set -euo pipefail
cd "$(dirname "$0")"

DB="" REAL=0 XUI_BIN="" BENCH=0 NOPG=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --db) DB=$2; shift 2 ;;
    --bench) BENCH=1; shift ;;
    --no-pg) NOPG=1; shift ;;
    --real-panel) REAL=1; if [[ ${2:-} && ${2:0:2} != -- ]]; then XUI_BIN=$2; shift; fi; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/xui-mult-preflight
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

step "syntax"
python3 -m py_compile xui_mult.py tests/*.py
bash -n install.sh.in
bash -n build.sh
echo "ok"

# --- a python that can talk to PostgreSQL, if there is a server to test against
PYT=python3
PG_NOTE="PostgreSQL tests skipped (--no-pg)"
if [[ $NOPG == 0 ]]; then
  if ! command -v initdb >/dev/null && ! ls /usr/lib/postgresql/*/bin/initdb >/dev/null 2>&1; then
    PG_NOTE="PostgreSQL tests skipped: no PostgreSQL server binaries (initdb) installed"
  elif [[ $EUID -eq 0 ]]; then
    PG_NOTE="PostgreSQL tests skipped: initdb refuses to run as root"
  elif python3 -c 'import psycopg2' 2>/dev/null || python3 -c 'import psycopg' 2>/dev/null; then
    PG_NOTE="PostgreSQL tests: on (driver from system python3)"
  else
    if [[ ! -x $CACHE/venv/bin/python ]]; then
      echo "creating a private venv with psycopg2-binary in $CACHE/venv (once)"
      mkdir -p "$CACHE"
      if python3 -m venv "$CACHE/venv" && "$CACHE/venv/bin/pip" install -q psycopg2-binary; then :
      else rm -rf "$CACHE/venv"; fi
    fi
    if [[ -x $CACHE/venv/bin/python ]]; then PYT=$CACHE/venv/bin/python; PG_NOTE="PostgreSQL tests: on (driver from $CACHE/venv)"
    else PG_NOTE="PostgreSQL tests skipped: no driver (apt install python3-psycopg2) and the venv could not be created"; fi
  fi
fi
echo "$PG_NOTE"

step "unit tests (mock of the v3.8.5 panel; every behaviour on SQLite and on PostgreSQL)"
"$PYT" -W ignore::ResourceWarning -m unittest discover -s tests -p 'test_xui_mult.py'

step "scale tests (1100 multiplied clients of 3000: exactness, statements per tick, idle cost, memory)"
"$PYT" -W ignore::ResourceWarning -m unittest discover -s tests -p 'test_scale.py'

step "build dist/install.sh"
bash build.sh
awk '/^__PAYLOAD_EOF__$/{f=0} f; /<<.__PAYLOAD_EOF__.$/{f=1}' dist/install.sh | base64 -d > "$TMP/payload.py"
if cmp -s "$TMP/payload.py" xui_mult.py; then echo "ok: installer payload is byte-identical to the tested xui_mult.py"
else echo "installer payload differs from xui_mult.py" >&2; exit 1; fi

if [[ -n $DB ]]; then
  if [[ $DB == postgres://* || $DB == postgresql://* || ( $DB != /* && $DB == *=* ) ]]; then
    step "compatibility with the PostgreSQL panel (READ-ONLY: nothing is written, no copy is possible)"
    REF=$DB
  else
    step "compatibility with $DB (checked on a temporary copy)"
    cp "$DB" "$TMP/x-ui.db"
    for ext in -wal -shm; do [[ -f $DB$ext ]] && cp "$DB$ext" "$TMP/x-ui.db$ext"; done
    REF=$TMP/x-ui.db
  fi
  "$PYT" - "$REF" <<'EOF'
import sys
sys.path.insert(0, ".")
import xui_mult as xm
db = xm.db_connect(sys.argv[1], ledger=False)   # schema check (refuses incompatible layouts); read-only
print("database     :", db.describe())
if db.kind == "sqlite":
    print("integrity    :", db.rows("PRAGMA integrity_check")[0][0], "(journal mode left as the panel set it)")
else:
    wanted = {"client_inbounds": "inbound_id", "clients": "email", "client_traffics": "email"}
    have = {(t, d) for t, d in db.rows("SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = current_schema()")}
    for t, col in wanted.items():
        ok = any(tt == t and f"({col}" in d.replace(", ", ",") for tt, d in have)
        print(f"index        : {t}({col}) " + ("present" if ok else "MISSING — ticks will scan the whole table"))
counts = xm.inbound_client_counts(db)
for ib in xm.all_inbounds(db):
    print(f"inbound #{ib['id']:<3}: {ib['protocol']}:{ib['port']}  {ib['remark'] or ''}  "
          f"({counts.get(ib['id'], (0, 0))[1]} clients)")
multi = db.rows("SELECT COUNT(*) FROM (SELECT client_id FROM client_inbounds GROUP BY client_id "
                "HAVING COUNT(*) > 1) x")[0][0]
print("clients on several inbounds:", multi, "(if one of them is multiplied, all their traffic is)")
print("ok: xui-mult can run against this database")
EOF
fi

if [[ $BENCH == 1 ]]; then
  step "benchmark"
  "$PYT" -W ignore::ResourceWarning tests/bench.py
fi

if [[ $REAL == 1 ]]; then
  if [[ -z $XUI_BIN ]]; then
    step "build 3X-UI v3.8.5 from source"
    XUI_BIN=$CACHE/x-ui-3.8.5
    if [[ ! -x $XUI_BIN ]]; then
      for t in git go gcc; do command -v $t >/dev/null || { echo "$t is required to build the panel" >&2; exit 1; }; done
      rm -rf "$CACHE/src"; mkdir -p "$CACHE"
      git clone -q --depth 1 --branch v3.8.5 https://github.com/MHSanaei/3x-ui "$CACHE/src"
      mkdir -p "$CACHE/src/internal/web/dist" && touch "$CACHE/src/internal/web/dist/.gitkeep"  # UI not needed
      (cd "$CACHE/src" && CGO_ENABLED=1 go build -o "$XUI_BIN" .)
    fi
    echo "panel binary: $XUI_BIN ($("$XUI_BIN" -v))"
  fi
  step "end-to-end against the real panel on SQLite"
  TMPDIR=$TMP "$PYT" -W ignore::ResourceWarning tests/e2e_real_panel.py "$XUI_BIN"
  if "$PYT" -c 'import sys; sys.path.insert(0, "tests"); import pgcluster; sys.exit(0 if pgcluster.available()[0] else 1)' 2>/dev/null && [[ $NOPG == 0 ]]; then
    step "end-to-end against the real panel on PostgreSQL"
    TMPDIR=$TMP "$PYT" -W ignore::ResourceWarning tests/e2e_real_panel.py "$XUI_BIN" --postgres
  else
    echo "(PostgreSQL end-to-end skipped: $PG_NOTE)"
  fi
fi

step "PRE-FLIGHT PASSED"
