"""XMR/USD pricing via Kraken public ticker. 5-min cache."""
import time
from decimal import Decimal
import httpx

_cached: tuple[float, Decimal] | None = None
_TTL = 300


async def xmr_per_usd() -> Decimal:
    """Return current XMR/USD as Decimal. Cached 5 min."""
    global _cached
    now = time.time()
    if _cached and (now - _cached[0]) < _TTL:
        return _cached[1]

    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get("https://api.kraken.com/0/public/Ticker", params={"pair": "XMRUSD"})
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"kraken: {data['error']}")
        result = data["result"]
        pair_key = next(iter(result))
        last_price = Decimal(result[pair_key]["c"][0])

    # Defensive sanity check: refuse to cache absurd / zero prices so a bad
    # ticker can't propagate into division-by-zero (or comically wrong XMR
    # amounts) in payments.create_payment.
    if last_price <= Decimal("1") or last_price > Decimal("100000"):
        raise RuntimeError(f"kraken returned implausible XMR/USD: {last_price}")

    _cached = (now, last_price)
    return last_price
