#!/usr/bin/env python3
"""Phala balance estimator + reload alert via ntfy.sh push.

Phala/Redpill has no balance API, so we track it locally:
- Operator records each PG card reload via `--seed <usd>` flag.
- We sum usage_log.cost_micro_usd since that load and divide by the markup
  factor to estimate Phala-side wholesale spend.
- Estimated current Phala balance = loaded - estimated_spent.
- If under REDPILL_RELOAD_ALERT_USD, push a notification to REDPILL_NTFY_TOPIC.

State file: /opt/phantom-api/data/phala-balance.json

Cron usage (every hour):
   0 * * * * /opt/phantom-api/venv/bin/python /opt/phantom-api/scripts/reload-alert.py

Initial seed (run once after first PG card load + Phala autopay setup):
   sudo -u phantom /opt/phantom-api/venv/bin/python /opt/phantom-api/scripts/reload-alert.py --seed 50

After each subsequent reload:
   sudo -u phantom /opt/phantom-api/venv/bin/python /opt/phantom-api/scripts/reload-alert.py --seed 50
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

# Cron runs without systemd EnvironmentFile loaded.
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

sys.path.insert(0, "/opt/phantom-api")

# Defer config import until after env loaded.
import sqlcipher3
from config import MICRO, MARKUP_NUM, DB_PATH

# Env names use REDPILL_* (the actual upstream gateway). Legacy PHALA_* still
# accepted for backwards compat with old .env files.
STATE_FILE = os.environ.get("REDPILL_STATE_FILE") or os.environ.get("PHALA_STATE_FILE", "/opt/phantom-api/data/redpill-balance.json")
NTFY_TOPIC = os.environ.get("REDPILL_NTFY_TOPIC") or os.environ.get("PHALA_NTFY_TOPIC", "")
NTFY_BASE  = os.environ.get("REDPILL_NTFY_BASE") or os.environ.get("PHALA_NTFY_BASE", "https://ntfy.sh")
ALERT_USD  = Decimal(os.environ.get("REDPILL_RELOAD_ALERT_USD") or os.environ.get("PHALA_RELOAD_ALERT_USD", "15"))
CHUNK_USD  = Decimal(os.environ.get("REDPILL_RELOAD_CHUNK_USD") or os.environ.get("PHALA_RELOAD_CHUNK_USD", "50"))
COOLDOWN_HRS = int(os.environ.get("REDPILL_ALERT_COOLDOWN_HOURS") or os.environ.get("PHALA_ALERT_COOLDOWN_HOURS", "6"))


def _open_db():
    pw = os.environ.get("PHANTOM_DB_PASSPHRASE")
    if not pw:
        print("ERR: PHANTOM_DB_PASSPHRASE not in env", file=sys.stderr)
        sys.exit(2)
    c = sqlcipher3.connect(DB_PATH)
    c.execute(f"PRAGMA key = '{pw}'")
    c.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return c


def _read_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def _write_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def _spend_since(conn, since_iso: str) -> int:
    """Sum of customer-side cost_micro_usd since the timestamp (marked-up dollars)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_micro_usd), 0) FROM usage_log WHERE created_at >= ?",
        (since_iso,),
    ).fetchone()
    return int(row[0] or 0)


def _estimate_balance_micro(state, conn) -> int:
    """Wholesale Phala balance estimate in micro-USD.
    Customer spend in cost_micro_usd is marked up by MARKUP_NUM/100. Divide
    back out to get the wholesale dollars that left our Phala balance."""
    loaded_micro = int(state["balance_at_load_micro"])
    customer_spend = _spend_since(conn, state["loaded_at"])
    wholesale_spend = int(customer_spend * 100 / MARKUP_NUM)
    return max(0, loaded_micro - wholesale_spend)


def _notify(message: str, title: str = "phantom reload"):
    if not NTFY_TOPIC:
        print(f"WARN: REDPILL_NTFY_TOPIC not set; would-have-sent: {message}", file=sys.stderr)
        return
    import urllib.request
    url = f"{NTFY_BASE}/{NTFY_TOPIC}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        method="POST",
        headers={"Title": title, "Priority": "high", "Tags": "moneybag"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        print(f"ERR: ntfy send failed: {type(e).__name__}: {e}", file=sys.stderr)


def cmd_seed(amount_usd: Decimal):
    state = {
        "loaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "balance_at_load_micro": int(amount_usd * MICRO),
        "last_alert_at": None,
    }
    _write_state(state)
    print(f"seeded: ${amount_usd} at {state['loaded_at']}")


def cmd_check():
    state = _read_state()
    if state is None:
        print("ERR: no state file. Run --seed <usd> after first PG card load.", file=sys.stderr)
        sys.exit(2)

    conn = _open_db()
    try:
        est_micro = _estimate_balance_micro(state, conn)
    finally:
        conn.close()

    est_usd = Decimal(est_micro) / Decimal(MICRO)
    print(f"estimated phala balance: ${est_usd:.2f} (loaded ${Decimal(state['balance_at_load_micro'])/Decimal(MICRO):.2f} at {state['loaded_at']})")

    if est_usd >= ALERT_USD:
        return

    # Cooldown so we don't spam if cron fires every hour but threshold stays low.
    last_alert = state.get("last_alert_at")
    if last_alert:
        delta = (datetime.now(timezone.utc) - datetime.fromisoformat(last_alert)).total_seconds() / 3600
        if delta < COOLDOWN_HRS:
            print(f"under threshold but in cooldown ({delta:.1f}h < {COOLDOWN_HRS}h)")
            return

    msg = (
        f"phantom phala balance ~${est_usd:.2f}\n"
        f"reload PG card with ${CHUNK_USD} XMR.\n"
        f"after reloading, run on VPS:\n"
        f"  sudo -u phantom /opt/phantom-api/venv/bin/python /opt/phantom-api/scripts/reload-alert.py --seed {CHUNK_USD}"
    )
    _notify(msg)
    state["last_alert_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_state(state)
    print(f"ALERT sent (balance ${est_usd:.2f} < ${ALERT_USD})")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=Decimal, help="record a fresh load of $N micro-USD")
    args = p.parse_args()
    if args.seed is not None:
        cmd_seed(args.seed)
    else:
        cmd_check()
