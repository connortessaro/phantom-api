#!/usr/bin/env bash
# Read-only wallet health probe. Run ON the VPS as root or via:
#   ssh phantom 'sudo /opt/phantom-api/scripts/wallet-check.sh'
#
# Verifies: services up, RPC auth works, daemon synced, balance readable,
# primary address resolves, poller has no recent errors, cold-sweep config sane.
# Never moves funds. Never creates payments. Safe to run anytime.

set -uo pipefail   # no -e: we want every probe to run + a summary at end

RUNTIME_DIR=/run/phantom
# HTTP digest auth password for monero-wallet-rpc. Lives in phantom-secrets.env
# alongside other unlock secrets. NOT phantom-wallet-pw (that's the wallet
# decryption password fed to monero-wallet-rpc --password-file).
SECRETS_FILE="$RUNTIME_DIR/phantom-secrets.env"
RPC_URL="http://127.0.0.1:18083/json_rpc"
RPC_USER="phantom"
ENV_FILE="/opt/phantom-api/.env"

# ─── coloring + counters ──────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; BOLD=$'\033[1m'; RST=$'\033[0m'
else
    GREEN=""; RED=""; YEL=""; BOLD=""; RST=""
fi
pass=0; fail=0; warn=0
ok()   { echo "  ${GREEN}PASS${RST} $*"; pass=$((pass+1)); }
bad()  { echo "  ${RED}FAIL${RST} $*";  fail=$((fail+1)); }
soft() { echo "  ${YEL}WARN${RST} $*";  warn=$((warn+1)); }
hdr()  { echo; echo "${BOLD}== $* ==${RST}"; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "ERR: must run as root (needs $SECRETS_FILE)" >&2
        exit 2
    fi
}
require_root

if [[ ! -r "$SECRETS_FILE" ]]; then
    echo "ERR: $SECRETS_FILE missing — run unlock.sh first" >&2
    exit 2
fi
RPC_PASS="$(grep -E '^WALLET_RPC_PASSWORD=' "$SECRETS_FILE" | head -1 | cut -d= -f2-)"
if [[ -z "$RPC_PASS" ]]; then
    echo "ERR: WALLET_RPC_PASSWORD not set in $SECRETS_FILE" >&2
    exit 2
fi

rpc() {
    local method="$1" params="${2:-{}}"
    curl -s --max-time 15 --digest -u "$RPC_USER:$RPC_PASS" \
        -H "Content-Type: application/json" \
        --data "{\"jsonrpc\":\"2.0\",\"id\":\"0\",\"method\":\"$method\",\"params\":$params}" \
        "$RPC_URL"
}

jpath() { python3 -c "import sys,json; d=json.load(sys.stdin); p='$1'.split('.');
for k in p:
    if k=='': continue
    d = d[int(k)] if k.isdigit() else d.get(k)
    if d is None: print('null'); sys.exit(0)
print(d if isinstance(d,(int,float,str,bool)) else json.dumps(d))" 2>/dev/null
}

# ─── 1. services ──────────────────────────────────────────────────────────────
hdr "1. systemd services"
for svc in monero-wallet-rpc tor; do
    state=$(systemctl is-active "$svc" 2>/dev/null || true)
    if [[ "$state" == "active" ]]; then
        ok "$svc is active"
    else
        bad "$svc is $state"
    fi
done

# ─── 2. RPC reachable + auth ──────────────────────────────────────────────────
hdr "2. wallet RPC auth"
resp=$(rpc get_version)
if [[ -z "$resp" ]]; then
    bad "no response from $RPC_URL — wallet RPC down or port unbound"
elif echo "$resp" | grep -q '"version"'; then
    ver=$(echo "$resp" | jpath result.version)
    ok "get_version OK (version=$ver)"
else
    bad "auth failed or RPC error: $(echo "$resp" | head -c 200)"
fi

# ─── 3. daemon synced ─────────────────────────────────────────────────────────
hdr "3. daemon height"
resp=$(rpc get_height)
height=$(echo "$resp" | jpath result.height)
if [[ "$height" =~ ^[0-9]+$ ]] && (( height > 0 )); then
    ok "wallet sees daemon at block $height"
    # Cross-check against public block explorer height — anything more than 10
    # blocks behind suggests wallet (or its daemon) is stale → confirmations stall.
    tip=$(curl -s --max-time 6 https://localmonero.co/blocks/api/get_stats 2>/dev/null \
          | python3 -c "import sys,json; print(json.load(sys.stdin).get('height',0))" 2>/dev/null || echo 0)
    if [[ "$tip" =~ ^[0-9]+$ ]] && (( tip > 0 )); then
        diff=$((tip - height))
        if (( diff < 0 )); then diff=$((-diff)); fi
        if (( diff <= 10 )); then
            ok "wallet within $diff blocks of public tip ($tip)"
        else
            soft "wallet is $diff blocks behind public tip ($tip) — may not be synced"
        fi
    fi
else
    bad "get_height failed: $(echo "$resp" | head -c 200)"
fi

# ─── 4. balance readable ──────────────────────────────────────────────────────
hdr "4. balance"
resp=$(rpc get_balance '{"account_index":0}')
if echo "$resp" | grep -q '"balance"'; then
    bal=$(echo "$resp" | jpath result.balance)
    unl=$(echo "$resp" | jpath result.unlocked_balance)
    btu=$(echo "$resp" | jpath result.blocks_to_unlock)
    xmr_bal=$(python3 -c "print(f'{$bal/1e12:.6f}')")
    xmr_unl=$(python3 -c "print(f'{$unl/1e12:.6f}')")
    ok "balance: $xmr_bal XMR (unlocked $xmr_unl, blocks_to_unlock=$btu)"
else
    bad "get_balance failed: $(echo "$resp" | head -c 200)"
fi

# ─── 5. primary address ───────────────────────────────────────────────────────
hdr "5. primary address"
resp=$(rpc get_address '{"account_index":0,"address_index":[0]}')
addr=$(echo "$resp" | jpath result.address)
if [[ "$addr" =~ ^4[1-9A-HJ-NP-Za-km-z]{94}$ ]]; then
    ok "primary hot address: $addr"
else
    bad "primary address malformed or missing: $addr"
fi

# ─── 6. poller errors ─────────────────────────────────────────────────────────
hdr "6. phantom-poller recent errors"
errs=$(journalctl -u phantom-poller -n 200 --no-pager --since "1 hour ago" 2>/dev/null \
       | grep -iE "error|exception|failed|traceback" | grep -vE "is fine|^--" | head -5 || true)
if [[ -z "$errs" ]]; then
    ok "no errors in poller logs (last 1h)"
else
    soft "poller log entries:"
    echo "$errs" | sed 's/^/      /'
fi

# ─── 7. cold-sweep config ─────────────────────────────────────────────────────
hdr "7. cold-sweep config"
if [[ ! -r "$ENV_FILE" ]]; then
    soft "cannot read $ENV_FILE (permissions)"
else
    cold=$(grep -E '^COLD_ADDRESS=' "$ENV_FILE" | head -1 | cut -d= -f2-)
    if [[ -z "$cold" ]]; then
        bad "COLD_ADDRESS not set — sweep-hot.py is a no-op"
    elif [[ "$cold" =~ ^4[1-9A-HJ-NP-Za-km-z]{94}$ ]]; then
        ok "COLD_ADDRESS is a primary Monero address (lineage safe)"
    elif [[ "$cold" =~ ^8[1-9A-HJ-NP-Za-km-z]{94}$ ]]; then
        soft "COLD_ADDRESS is a SUBADDRESS — funds linkable via master view key. Prefer primary (starts with 4)."
    else
        bad "COLD_ADDRESS malformed: $cold"
    fi
    # cron sanity
    if sudo -u phantom crontab -l 2>/dev/null | grep -q sweep-hot; then
        ok "sweep-hot cron installed"
    else
        soft "no sweep-hot cron entry (will not sweep automatically)"
    fi
fi

# ─── summary ──────────────────────────────────────────────────────────────────
hdr "summary"
echo "  ${GREEN}${pass} pass${RST}, ${YEL}${warn} warn${RST}, ${RED}${fail} fail${RST}"
if (( fail > 0 )); then
    exit 1
fi
