#!/usr/bin/env python3
"""Encrypted SQLCipher backup using the online .backup API.

The bash predecessor used `cp` against a WAL-mode database, which only
captured the main page file (4 KB stub) and dropped the WAL where the
actual data lives. This used the sqlcipher3 Connection.backup() method
which streams pages from the live DB into a new encrypted DB. The result
is a self-contained file that opens with the same passphrase.

Cron (every hour, runs as phantom user):
   5 * * * * /opt/phantom-api/venv/bin/python /opt/phantom-api/scripts/backup-db.py
"""
import os
import sys
import time
from datetime import datetime, timezone

# Cron has minimal env. Pull secrets from tmpfs.
def _load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

_load_env_file("/opt/phantom-api/.env")
_load_env_file("/run/phantom/phantom-secrets.env")

import sqlcipher3

DB_PATH = os.environ.get("DB_PATH", "/opt/phantom-api/data/phantom.db")
BACKUP_DIR = os.environ.get("PHANTOM_BACKUP_DIR", "/opt/phantom-api/data/backups")
KEEP = int(os.environ.get("PHANTOM_BACKUP_KEEP", "24"))

passphrase = os.environ.get("PHANTOM_DB_PASSPHRASE")
if not passphrase:
    print("ERR: PHANTOM_DB_PASSPHRASE not in env (tmpfs not unlocked?)", file=sys.stderr)
    sys.exit(2)

os.makedirs(BACKUP_DIR, exist_ok=True)
os.chmod(BACKUP_DIR, 0o700)

stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out_path = os.path.join(BACKUP_DIR, f"phantom-{stamp}.db")
tmp_path = out_path + ".partial"

# Open source w/ passphrase, open empty destination w/ same passphrase,
# stream-copy all pages. Online-safe — does not block writers.
src = sqlcipher3.connect(DB_PATH)
src.execute(f"PRAGMA key = '{passphrase}'")
src.execute("SELECT count(*) FROM sqlite_master").fetchone()  # validate key

# Remove any leftover partial from a previous crashed run.
if os.path.exists(tmp_path):
    os.remove(tmp_path)

dst = sqlcipher3.connect(tmp_path)
dst.execute(f"PRAGMA key = '{passphrase}'")
# Force rollback journal (not WAL) on the destination so the backup is a
# single self-contained file with no -wal / -shm sidecars to forget.
dst.execute("PRAGMA journal_mode = DELETE")

try:
    src.backup(dst)
finally:
    dst.close()
    src.close()

# In case any sidecars slipped in (older sqlcipher behavior), drop them.
for sidecar in (tmp_path + "-wal", tmp_path + "-shm"):
    if os.path.exists(sidecar):
        os.remove(sidecar)

os.replace(tmp_path, out_path)

# Quick self-verify: reopen, switch to rollback journal so verify doesn't
# leave a -wal sidecar, count tables + rows.
verify = sqlcipher3.connect(out_path)
verify.execute(f"PRAGMA key = '{passphrase}'")
verify.execute("PRAGMA journal_mode = DELETE")
tables = [r[0] for r in verify.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
).fetchall()]
counts = {t: verify.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
verify.execute("PRAGMA wal_checkpoint(TRUNCATE)")
verify.close()

# Final cleanup: drop any sidecars created during the verify open.
for sidecar in (out_path + "-wal", out_path + "-shm", out_path + "-journal"):
    if os.path.exists(sidecar):
        os.remove(sidecar)
os.chmod(out_path, 0o400)

if not tables:
    print(f"FAIL: backup verified open but contains no tables. Refusing to keep empty backup.",
          file=sys.stderr)
    os.remove(out_path)
    sys.exit(3)

# Prune: keep KEEP most recent.
files = sorted(
    (f for f in os.listdir(BACKUP_DIR) if f.startswith("phantom-") and f.endswith(".db")),
    reverse=True,
)
for old in files[KEEP:]:
    try:
        os.remove(os.path.join(BACKUP_DIR, old))
    except OSError:
        pass

print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} backup ok -> {out_path}")
print(f"  tables: {tables}")
print(f"  rows: {counts}")
