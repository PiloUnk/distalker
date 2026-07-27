#!/usr/bin/env bash
#
# Build a release ZIP for Dispatcharr's "Import Plugin" button.
#
#   ./build.sh            -> dist/distalker-<version>.zip
#
# The version comes from plugin.json, so it never drifts from what the UI
# reports after installing.
set -euo pipefail

cd "$(dirname "$0")"

NAME=distalker
VERSION=$(python3 -c "import json; print(json.load(open('plugin.json'))['version'])")
OUT="dist/${NAME}-${VERSION}.zip"

if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    echo "warning: working tree is dirty -- the archive is built from the last"
    echo "         commit (HEAD), so uncommitted changes will NOT be included."
    echo
fi

mkdir -p dist
rm -f "$OUT"

# The archive MUST contain a top-level "<name>/" directory.
#
# Dispatcharr derives the plugin key from the directory holding plugin.py, and
# falls back to the ZIP *filename* when the archive is flat. Its sanitiser also
# rewrites hyphens and dots to underscores, so a flat "distalker-0.1.0.zip"
# would install under the key "distalker_0_1_0" -- which breaks the scheduled
# sync task, since that looks the plugin up by the key "distalker".
#
# Using git archive also guarantees only committed, tracked files ship: no
# __pycache__, no stray local M3U files.
git archive --format=zip --prefix="${NAME}/" -o "$OUT" HEAD

python3 - "$OUT" "$NAME" <<'PY'
import hashlib
import json
import sys
import zipfile

path, name = sys.argv[1], sys.argv[2]

with zipfile.ZipFile(path) as zf:
    names = zf.namelist()
    required = [f"{name}/plugin.py", f"{name}/plugin.json", f"{name}/resolver.py",
                f"{name}/stalker_api.py", f"{name}/sync.py", f"{name}/tasks.py"]
    missing = [r for r in required if r not in names]
    if missing:
        sys.exit(f"ERROR: archive is missing {missing}")

    # The key Dispatcharr will install under, mirroring _sanitize_plugin_key.
    manifest = json.loads(zf.read(f"{name}/plugin.json"))

digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
size = len(open(path, "rb").read())

print(f"built    {path}")
print(f"plugin   {manifest['name']} {manifest['version']}")
print(f"key      {name}")
print(f"files    {len(names)}")
print(f"size     {size / 1024:.1f} KiB")
print(f"sha256   {digest}")
PY
