#!/usr/bin/env bash
# Nightly mesh.db backup. Snapshots locally first (sqlite .backup straight onto a
# CIFS share can hang), gzips, copies to BACKUP_DIR, keeps 14 days.
set -euo pipefail
SRC=~/.local/share/meshcore-bridge/mesh.db
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
DEST=${BACKUP_DIR:-$HOME/meshcore-backups}
OUT=mesh-$(date +%F).db
~/meshcore-bridge/venv/bin/python - "$SRC" "$TMP/$OUT" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
PY
gzip "$TMP/$OUT"
mkdir -p "$DEST"
cp "$TMP/$OUT.gz" "$DEST/"
find "$DEST" -name 'mesh-*.db.gz' -mtime +14 -delete
echo "backed up $(du -h "$DEST/$OUT.gz" | cut -f1) to $DEST/$OUT.gz"
