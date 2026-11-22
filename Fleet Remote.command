#!/bin/bash
# Fleet Remote launcher (Mac)
# Double-click this file in Finder (once: chmod +x "Fleet Remote.command").
# It starts the newest fleet_remote_v<version>.py in this folder, so the Update
# button inside the app can swap the versioned file without breaking anything.
cd "$(dirname "$0")" || exit 1
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi
SCRIPT=$("$PY" - <<'EOF'
import re, glob
best = None
for p in glob.glob("fleet_remote_v*.py"):
    m = re.match(r"^fleet_remote_v(\d+)\.(\d+)\.py$", p)
    if m:
        v = (int(m.group(1)), int(m.group(2)))
        if best is None or v > best[0]:
            best = (v, p)
print(best[1] if best else "")
EOF
)
if [ -z "$SCRIPT" ]; then
  echo "No fleet_remote_v*.py found in $(pwd)"
  read -n 1 -s -r -p "Press any key to close"
  exit 1
fi
exec "$PY" "$SCRIPT" "$@"
