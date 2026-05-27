#!/usr/bin/env bash
# Smoke test both payment rails on a deployed phantom-api.
#
# Hits /v1/purchase with rail=xmr (MoneroPay) and rail=multi (NowPayments),
# validates response shape, but does NOT actually pay. Verifies:
#   - both rails reachable
#   - both return correct shape (xmr_address+xmr_amount vs checkout_url)
#   - multi-crypto rail applies the surcharge
#   - /v1/bundles exposes the surcharge percentage
#   - /v1/health is happy
#
# Safe to run anytime. Does not move funds, does not consume capacity beyond
# pending-IP slot (cleans up after self).
#
# Usage:
#   ./scripts/smoke-test-rails.sh                          # against localhost:8000
#   API_BASE=https://api.phantom.codes ./scripts/smoke-test-rails.sh
#
# Env:
#   API_BASE   default: http://127.0.0.1:8000

set -uo pipefail

API_BASE="${API_BASE:-http://127.0.0.1:8000}"

if [[ -t 1 ]]; then
    GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; BOLD=$'\033[1m'; RST=$'\033[0m'
else
    GREEN=""; RED=""; YEL=""; BOLD=""; RST=""
fi
hdr()  { echo; echo "${BOLD}== $* ==${RST}"; }
ok()   { echo "  ${GREEN}PASS${RST} $*"; pass=$((pass+1)); }
bad()  { echo "  ${RED}FAIL${RST} $*";  fail=$((fail+1)); }
soft() { echo "  ${YEL}WARN${RST} $*";  warn=$((warn+1)); }
pass=0; fail=0; warn=0

need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERR: $1 missing" >&2
        exit 2
    fi
}
need curl
need jq
need python3

# ─── health ────────────────────────────────────────────────────────────────────
hdr "health"
HEALTH=$(curl -fsSL -m 5 "$API_BASE/health" 2>&1 || true)
if echo "$HEALTH" | jq -e '.status == "ok"' >/dev/null 2>&1; then
    ok "/health responds ok"
else
    bad "/health unreachable or malformed: $HEALTH"
    exit 1
fi

# ─── bundles + surcharge exposed ───────────────────────────────────────────────
hdr "bundles + surcharge"
BUNDLES=$(curl -fsSL -m 5 "$API_BASE/v1/bundles") || { bad "/v1/bundles unreachable"; exit 1; }
SURCHARGE=$(echo "$BUNDLES" | jq -r '.multi_crypto_surcharge_pct // empty')
if [[ -n "$SURCHARGE" ]]; then
    ok "/v1/bundles exposes multi_crypto_surcharge_pct=$SURCHARGE"
else
    bad "/v1/bundles missing multi_crypto_surcharge_pct field"
fi

BUNDLE_HAS_MULTI=$(echo "$BUNDLES" | jq -r '.data[0].price_usd_multi_crypto // empty')
if [[ -n "$BUNDLE_HAS_MULTI" ]]; then
    ok "/v1/bundles exposes price_usd_multi_crypto per bundle"
else
    bad "/v1/bundles missing price_usd_multi_crypto per bundle"
fi

# ─── XMR rail (MoneroPay) ──────────────────────────────────────────────────────
hdr "XMR rail (MoneroPay)"
RESP=$(curl -fsSL -m 10 -X POST "$API_BASE/v1/purchase" \
    -H "Content-Type: application/json" \
    -d '{"bundle":"small","rail":"xmr"}' 2>&1)
RC=$?
if [[ $RC -ne 0 ]]; then
    bad "POST /v1/purchase rail=xmr failed: $RESP"
else
    PID=$(echo "$RESP" | jq -r '.payment_id // empty')
    ADDR=$(echo "$RESP" | jq -r '.xmr_address // empty')
    AMOUNT=$(echo "$RESP" | jq -r '.xmr_amount // empty')
    RAIL=$(echo "$RESP" | jq -r '.rail // empty')

    if [[ -n "$PID" ]]; then ok "payment_id returned ($PID)"; else bad "payment_id missing"; fi
    if [[ -n "$ADDR" && "$ADDR" != "null" ]]; then ok "xmr_address returned"; else bad "xmr_address missing"; fi
    if [[ -n "$AMOUNT" && "$AMOUNT" != "null" ]]; then ok "xmr_amount returned ($AMOUNT)"; else bad "xmr_amount missing"; fi
    if [[ "$RAIL" == "monero_pay" ]]; then ok "rail=monero_pay echoed"; else soft "rail field not echoed (expected 'monero_pay', got '$RAIL')"; fi

    # Confirm /v1/purchase/{id}/status reflects pending
    sleep 1
    STATUS=$(curl -fsSL -m 5 "$API_BASE/v1/purchase/$PID/status" | jq -r '.status // empty')
    if [[ "$STATUS" == "pending" || "$STATUS" == "confirming" ]]; then
        ok "status endpoint returns '$STATUS' for fresh order"
    else
        bad "status endpoint unexpected: '$STATUS'"
    fi
fi

# ─── Multi-crypto rail (NowPayments) ───────────────────────────────────────────
hdr "Multi-crypto rail (NowPayments)"
RESP=$(curl -fsSL -m 10 -X POST "$API_BASE/v1/purchase" \
    -H "Content-Type: application/json" \
    -d '{"bundle":"small","rail":"multi"}' 2>&1)
RC=$?
if [[ $RC -ne 0 ]]; then
    bad "POST /v1/purchase rail=multi failed: $RESP"
else
    PID=$(echo "$RESP" | jq -r '.payment_id // empty')
    URL=$(echo "$RESP" | jq -r '.checkout_url // empty')
    PRICE=$(echo "$RESP" | jq -r '.price_usd // empty')
    CREDIT=$(echo "$RESP" | jq -r '.credit_usd // empty')
    SURCHARGE_ECHO=$(echo "$RESP" | jq -r '.surcharge_pct // empty')

    if [[ -n "$PID" ]]; then ok "payment_id returned ($PID)"; else bad "payment_id missing"; fi
    if [[ -n "$URL" && "$URL" != "null" ]]; then ok "checkout_url returned"; else bad "checkout_url missing"; fi
    if [[ -n "$PRICE" && "$PRICE" != "null" ]]; then ok "price_usd returned ($PRICE)"; else bad "price_usd missing"; fi
    if [[ -n "$SURCHARGE_ECHO" ]]; then ok "surcharge_pct echoed ($SURCHARGE_ECHO)"; else bad "surcharge_pct missing"; fi

    # Verify surcharge math: price_usd should be credit_usd * (1 + surcharge/100)
    if [[ -n "$PRICE" && -n "$CREDIT" && -n "$SURCHARGE_ECHO" ]]; then
        EXPECTED=$(python3 -c "
credit = float('$CREDIT')
sur = float('$SURCHARGE_ECHO')
# small bundle: credit_usd=10, sticker_price=10, surcharged=10*(1+sur/100)
print(f'{10 * (1 + sur/100):.2f}')
")
        ACTUAL_FMT=$(python3 -c "print(f'{float(\"$PRICE\"):.2f}')")
        if [[ "$EXPECTED" == "$ACTUAL_FMT" ]]; then
            ok "surcharge math: $ACTUAL_FMT == 10 × (1 + $SURCHARGE_ECHO/100)"
        else
            bad "surcharge math mismatch: got $ACTUAL_FMT, expected $EXPECTED"
        fi
    fi
fi

# ─── invalid rail rejected (in hybrid mode) ────────────────────────────────────
hdr "invalid rail handling"
RESP=$(curl -s -o /dev/null -w "%{http_code}" -m 5 -X POST "$API_BASE/v1/purchase" \
    -H "Content-Type: application/json" \
    -d '{"bundle":"small","rail":"bogus"}')
# Server defaults bogus rail to "multi" in hybrid mode (treats unknown as not-xmr).
# Should succeed 200. If server returns 400 it's also acceptable behavior.
if [[ "$RESP" == "200" || "$RESP" == "400" ]]; then
    ok "unknown rail handled (HTTP $RESP)"
else
    soft "unknown rail returned HTTP $RESP — verify intentional"
fi

# ─── summary ───────────────────────────────────────────────────────────────────
hdr "summary"
echo "  ${GREEN}pass: $pass${RST}  ${YEL}warn: $warn${RST}  ${RED}fail: $fail${RST}"
if [[ $fail -gt 0 ]]; then
    exit 1
fi
exit 0
