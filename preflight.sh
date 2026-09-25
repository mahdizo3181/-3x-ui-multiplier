#!/usr/bin/env bash
# Pre-flight checks before deploying xui-mult. Runs locally and only writes to dist/ and temp dirs.
#   bash preflight.sh                       syntax, unit tests, build, installer payload == tested source
#   bash preflight.sh --db ./x-ui.db        + compatibility check on a COPY of your production database
#   bash preflight.sh --real-panel [X-UI]   + end-to-end run against a real 3X-UI binary; without a path,
#                                             v3.8.5 is built from source (needs git, go and gcc)
set -euo pipefail
cd "$(dirname "$0")"

DB="" REAL=0 XUI_BIN=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --db) DB=$2; shift 2 ;;
    --real-panel) REAL=1; if [[ ${2:-} && ${2:0:2} != -- ]]; then XUI_BIN=$2; shift; fi; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

step "syntax"
python3 -m py_compile xui_mult.py tests/*.py
bash -n install.sh.in
bash -n build.sh
echo "ok"

step "unit tests (mock of the v3.8.5 panel)"
python3 -W ignore::ResourceWarning -m unittest discover -s tests

step "build dist/install.sh"
bash build.sh
awk '/^__PAYLOAD_EOF__$/{f=0} f; /<<.__PAYLOAD_EOF__.$/{f=1}' dist/install.sh | base64 -d > "$TMP/payload.py"
if cmp -s "$TMP/payload.py" xui_mult.py; then echo "ok: installer payload is byte-identical to the tested xui_mult.py"
else echo "installer payload differs from xui_mult.py" >&2; exit 1; fi

if [[ -n $DB ]]; then
  step "compatibility with $DB (checked on a temporary copy)"
  cp "$DB" "$TMP/x-ui.db"
  for ext in -wal -shm; do [[ -f $DB$ext ]] && cp "$DB$ext" "$TMP/x-ui.db$ext"; done
  python3 - "$TMP/x-ui.db" <<'EOF'
import sys
sys.path.insert(0, ".")
import xui_mult as xm
path = sys.argv[1]
conn = xm.db_connect(path)                      # schema check (refuses incompatible layouts)
print("integrity     :", xm.q1(conn, "PRAGMA integrity_check")[0])
print("journal mode  :", xm.q1(conn, "PRAGMA journal_mode")[0], "(left as the panel set it)")
url, domain = xm.detect_panel_url(conn)
print("panel API URL :", url)
print("panel domain  :", domain or "(none)", "-> API Host header" if domain else "")
print("inbounds      :", ", ".join(f"#{i['id']} {i['protocol']}:{i['port']}" for i in xm.all_inbounds(conn)) or "none")
print("clients       :", xm.q1(conn, "SELECT COUNT(*) FROM clients")[0])
neg = xm.q1(conn, "SELECT COUNT(*) FROM clients WHERE expiry_time < 0")[0]
print("delayed start :", neg, "client(s) with 'start after first use'")
print("ok: xui-mult can run against this database")
EOF
fi

if [[ $REAL == 1 ]]; then
  if [[ -z $XUI_BIN ]]; then
    step "build 3X-UI v3.8.5 from source"
    CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/xui-mult-preflight
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
  step "end-to-end against the real panel"
  TMPDIR=$TMP python3 -W ignore::ResourceWarning tests/e2e_real_panel.py "$XUI_BIN"
fi

step "PRE-FLIGHT PASSED"
