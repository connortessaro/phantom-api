"""Background worker. Polls wallet for incoming payments, transitions states.

Run as systemd unit. pending -> confirming -> ready. Issuance happens at /v1/purchase/{id}/status (atomic).
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import payments
from config import DB_PATH, confirmations_for_payment

POLL_INTERVAL_SEC = 30

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s: %(message)s")


async def _daemon_height() -> int:
    res = await payments.rpc("get_height", {})
    return int(res.get("height", 0))


async def tick():
    """One sweep over open payments. Uses incoming_transfers so self-sends
    (stagenet test) and external sends both surface under destination subaddr."""
    async with db._lock:
        rows = db.conn().execute(
            "SELECT payment_id, xmr_address, xmr_subaddr_index, xmr_amount, status, expires_at, "
            "bundle_name, credit_micro_usd "
            "FROM payments WHERE status IN ('pending', 'confirming')"
        ).fetchall()

    if not rows:
        return

    try:
        daemon_height = await _daemon_height()
    except Exception as e:
        logging.error(f"daemon_height failed: {type(e).__name__}")
        return

    now = datetime.now(timezone.utc)
    for payment_id, addr, subaddr_index, expected, status, expires, bundle_name, credit_micro in rows:
        if datetime.fromisoformat(expires) < now and status == "pending":
            async with db._lock:
                db.conn().execute(
                    "UPDATE payments SET status = 'expired' WHERE payment_id = ? AND status = 'pending'",
                    (payment_id,),
                )
                db.conn().commit()
            continue

        try:
            res = await payments.rpc("incoming_transfers", {
                "transfer_type": "all",
                "account_index": 0,
                "subaddr_indices": [subaddr_index],
            })
        except Exception as e:
            logging.error(f"incoming_transfers failed: {type(e).__name__}")
            continue

        received_pico = 0
        confirmations = 0
        for out in res.get("transfers", []) or []:
            if out.get("subaddr_index", {}).get("minor") != subaddr_index:
                continue
            received_pico += int(out.get("amount", 0))
            block_h = int(out.get("block_height", 0))
            if block_h > 0:
                confs = max(0, daemon_height - block_h + 1)
                confirmations = max(confirmations, confs)
            # block_height 0 = still in mempool, 0 confirmations

        expected_pico = payments.xmr_to_piconero(Decimal(expected))
        required_confs = confirmations_for_payment(bundle_name, int(credit_micro))
        if received_pico >= int(expected_pico * 98 // 100):
            if confirmations >= required_confs:
                async with db._lock:
                    db.conn().execute(
                        "UPDATE payments SET status = 'ready' "
                        "WHERE payment_id = ? AND status IN ('pending', 'confirming')",
                        (payment_id,),
                    )
                    db.conn().commit()
            else:
                async with db._lock:
                    db.conn().execute(
                        "UPDATE payments SET status = 'confirming' "
                        "WHERE payment_id = ? AND status = 'pending'",
                        (payment_id,),
                    )
                    db.conn().commit()


async def main():
    await db.init_db(DB_PATH)
    while True:
        try:
            await tick()
        except Exception as e:
            logging.error(f"tick error: {type(e).__name__}")
        await asyncio.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
