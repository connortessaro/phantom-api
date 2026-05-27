"""Run on operator PC. Sweeps wallet float above SWEEP_THRESHOLD_XMR to cold address."""
import os
import asyncio
from decimal import Decimal

import httpx

WALLET_RPC_URL = os.environ.get("LOCAL_WALLET_RPC", "http://127.0.0.1:18083/json_rpc")
WALLET_USER = os.environ.get("WALLET_RPC_USER", "phantom")
WALLET_PASS = os.environ["WALLET_RPC_PASSWORD"]
COLD_ADDRESS = os.environ["COLD_ADDRESS"]
SWEEP_THRESHOLD_XMR = Decimal(os.environ.get("SWEEP_THRESHOLD_XMR", "2"))


async def rpc(method, params=None):
    payload = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
    async with httpx.AsyncClient(
        timeout=60, auth=httpx.DigestAuth(WALLET_USER, WALLET_PASS),
    ) as c:
        r = await c.post(WALLET_RPC_URL, json=payload)
        r.raise_for_status()
        d = r.json()
        if "error" in d:
            raise RuntimeError(d["error"])
        return d["result"]


async def main():
    bal = await rpc("get_balance", {"account_index": 0})
    unlocked = Decimal(bal.get("unlocked_balance", 0)) / Decimal("1e12")
    print(f"Unlocked: {unlocked} XMR")
    if unlocked <= SWEEP_THRESHOLD_XMR:
        print("Below threshold; nothing to sweep.")
        return
    to_sweep = unlocked - SWEEP_THRESHOLD_XMR
    print(f"Sweeping {to_sweep} XMR to cold address...")
    # `sweep_all` to a single dest with priority normal
    res = await rpc("sweep_all", {
        "account_index": 0,
        "address": COLD_ADDRESS,
        "priority": 1,
        "do_not_relay": False,
    })
    print(res)


if __name__ == "__main__":
    asyncio.run(main())
