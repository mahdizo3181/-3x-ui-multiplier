#!/usr/bin/env bash
# Builds dist/install.sh: the installer with xui_mult.py embedded (single file to copy to a server).
set -euo pipefail
cd "$(dirname "$0")"
python3 -m py_compile xui_mult.py
VER=$(python3 -c 'import re;print(re.search(r"^VERSION = \"(.+?)\"", open("xui_mult.py").read(), re.M).group(1))')
mkdir -p dist
python3 - "$VER" <<'EOF'
import base64, sys, textwrap
tpl = open("install.sh.in").read()
payload = "\n".join(textwrap.wrap(base64.b64encode(open("xui_mult.py", "rb").read()).decode(), 76))
open("dist/install.sh", "w").write(tpl.replace("__VERSION__", sys.argv[1]).replace("__PAYLOAD__", payload))
EOF
chmod 755 dist/install.sh
bash -n dist/install.sh
echo "built dist/install.sh (xui-mult $VER, $(wc -c < dist/install.sh) bytes)"
