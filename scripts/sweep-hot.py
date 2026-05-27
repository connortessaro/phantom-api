#!/usr/bin/env python3
"""Hot wallet sweep — moves funds from VPS hot wallet to operator's cold address
when unlocked balance crosses HOT_SWEEP_THRESHOLD_USD (priced against live
XMR/USD). Run via cron every ~10min.

Caps blast radius: if VPS gets owned, attacker steals at most one threshold worth.
COLD_ADDRESS must be set in .env. Refuses to run if unset (fail-safe — better to
accumulate than to sweep to a wrong/empty destination).

If price fetch fails, falls back to HOT_SWEEP_THRESHOLD_XMR (hard XMR floor)
so the sweeper still runs during Kraken outages."""
import asyncio
import os
import sys
from decimal import Decimal

# Cron runs without the systemd EnvironmentFile loaded, so source both .env
# and the tmpfs secrets file ourselves before importing config (which insists
# on REDPILL_API_KEY etc. at module load).
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

# Re-use phantom config + rpc helper.
sys.path.insert(0, "/opt/phantom-api")
from config import COLD_ADDRESS, HOT_SWEEP_THRESHOLD_USD, HOT_SWEEP_THRESHOLD_XMR
from payments import rpc, PICONERO
import pricing


STATE_FILE = "/opt/phantom-api/wallet/sweep-state.txt"


def _read_last_total() -> int:
    """Last observed total balance, in piconero. 0 if first run."""
    try:
        with open(STATE_FILE) as f:
            return int(f.read().strip() or 0)
    except FileNotFoundError:
        return 0


def _write_last_total(total: int):
    with open(STATE_FILE, "w") as f:
        f.write(str(total))


async def main() -> int:
    if not COLD_ADDRESS:
        print("ERR: COLD_ADDRESS not set in .env. Refusing to sweep.", file=sys.stderr)
        return 2

    threshold_usd = Decimal(HOT_SWEEP_THRESHOLD_USD)
    try:
        usd_per_xmr = await pricing.xmr_per_usd()
        threshold_xmr = threshold_usd / usd_per_xmr
        print(f"price: ${usd_per_xmr}/XMR, threshold: ${threshold_usd} = {threshold_xmr:.6f} XMR")
    except Exception as e:
        threshold_xmr = Decimal(HOT_SWEEP_THRESHOLD_XMR)
        print(
            f"WARN: price fetch failed ({e}); falling back to {threshold_xmr} XMR floor",
            file=sys.stderr,
        )
    threshold_pico = int(threshold_xmr * PICONERO)

    bal = await rpc("get_balance", {"account_index": 0})
    unlocked = int(bal.get("unlocked_balance", 0))
    total    = int(bal.get("balance", 0))

    # Anomaly: balance dropped without us sweeping. Means either a confirmed
    # outflow we didn't initiate (= compromise) or a reorg. Either way, ALERT.
    last_total = _read_last_total()
    if last_total > total + threshold_pico:
        print(
            f"ANOMALY: balance dropped {(last_total - total) / PICONERO:.6f} XMR "
            f"since last check (last={last_total / PICONERO:.6f}, now={total / PICONERO:.6f}). "
            f"Possible compromise or reorg. Investigate before next sweep.",
            file=sys.stderr,
        )
        # Don't sweep on suspected compromise. Operator must clear state file
        # by hand to resume: `rm /opt/phantom-api/wallet/sweep-state.txt`
        return 3

    print(f"hot balance: total={total / PICONERO:.6f} XMR, unlocked={unlocked / PICONERO:.6f} XMR")

    if unlocked < threshold_pico:
        print(f"below threshold ({threshold_xmr:.6f} XMR ≈ ${threshold_usd}). No sweep.")
        _write_last_total(total)
        return 0

    print(f"sweeping all unlocked → {COLD_ADDRESS[:12]}...{COLD_ADDRESS[-6:]}")
    res = await rpc(
        "sweep_all",
        {
            "address": COLD_ADDRESS,
            "account_index": 0,
            # do_not_relay=false (default) — broadcasts the tx
            # priority 1 = normal fee tier
            "priority": 1,
            "ring_size": 16,
        },
    )
    tx_hashes = res.get("tx_hash_list", [])
    amounts   = res.get("amount_list", [])
    fees      = res.get("fee_list", [])
    for h, a, f in zip(tx_hashes, amounts, fees):
        print(f"  tx={h} sent={a / PICONERO:.6f} fee={f / PICONERO:.6f}")
    # Re-query post-sweep balance so the next run's anomaly check has a clean baseline
    # that already accounts for the outflow we just performed.
    post = await rpc("get_balance", {"account_index": 0})
    _write_last_total(int(post.get("balance", 0)))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
