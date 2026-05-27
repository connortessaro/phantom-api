"""Shared fixtures + env setup. Loaded automatically by pytest."""
import os
import sys
import pathlib

# Ensure project root on sys.path so `import config` etc. works.
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Tests must not depend on real .env. Set minimum required vars before any imports.
os.environ.setdefault("REDPILL_API_KEY", "test-redpill-key")
os.environ.setdefault("WALLET_ONION", "testonion.onion")
os.environ.setdefault("WALLET_RPC_PASSWORD", "test-rpc-pass")
os.environ.setdefault("PHANTOM_DB_PASSPHRASE", "test-db-pass-do-not-use-prod")
os.environ.setdefault("DB_PATH", ":memory:")

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    """Open a clean SQLCipher DB on disk per test. Yields db module.
    SQLCipher's :memory: mode doesn't share state across connection objects, so we
    use a tmp file (auto-cleaned by pytest's tmp_path)."""
    import db as _db
    db_path = str(tmp_path / "test.db")
    # Reset module-level state so init_db can run again
    _db._conn = None
    # init_db pops PHANTOM_DB_PASSPHRASE from env — re-set for each test
    os.environ["PHANTOM_DB_PASSPHRASE"] = "test-db-pass-do-not-use-prod"
    await _db.init_db(db_path)
    yield _db
    try:
        _db._conn.close()
    except Exception:
        pass
    _db._conn = None
