#!/usr/bin/env bash
# Phase 0 smoke test for Redpill (Phala's gateway) Confidential AI.
# Usage:
#   export REDPILL_API_KEY=sk-...   # legacy PHALA_API_KEY still accepted
#   ./phase-0-smoke.sh
#
# Verifies: auth works, chat completions returns, usage block present,
# attestation reachable, per-request signature reachable.
# Costs <$0.001 — uses cheapest Phala-TEE model with a tiny prompt.

set -uo pipefail   # no -e: we want soft failures, summary at end

BASE="https://api.redpill.ai/v1"
MODEL="phala/qwen-2.5-7b-instruct"

KEY="${REDPILL_API_KEY:-${PHALA_API_KEY:-}}"
if [[ -z "$KEY" ]]; then
  echo "ERR: set REDPILL_API_KEY (or legacy PHALA_API_KEY) first" >&2
  exit 1
fi

AUTH="Authorization: Bearer ${KEY}"
JSON='Content-Type: application/json'
NONCE=$(openssl rand -hex 32)
OUTDIR="$(cd "$(dirname "$0")/.." && pwd)/notes/phase-0-output"
mkdir -p "$OUTDIR"

bold() { printf "\n\033[1m== %s ==\033[0m\n" "$*"; }
preview() { python3 -c "import sys,json; d=json.load(open('$1')); print(json.dumps(d, indent=2)[:1500])" 2>/dev/null || head -c 1500 "$1"; echo; }

bold "1. Account usage check (/v1/usage) — non-fatal"
curl -sS -w "\nHTTP %{http_code}\n" "$BASE/usage" -H "$AUTH" -o "$OUTDIR/01-usage.json"
cat "$OUTDIR/01-usage.json"; echo

bold "2. Models catalog (/v1/models) — saving full, showing phala/* + openai/gpt-oss-* only"
curl -fsS "$BASE/models" -H "$AUTH" -o "$OUTDIR/02-models.json"
MODELS_JSON="$OUTDIR/02-models.json"
python3 - "$MODELS_JSON" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for m in d.get("data", []):
    if m["id"].startswith("phala/") or "gpt-oss" in m["id"]:
        p = m.get("pricing", {})
        try:
            inp = float(p.get("prompt", 0)) * 1e6
            out = float(p.get("completion", 0)) * 1e6
        except Exception:
            inp = out = 0
        print(f"{m['id']:55} ctx={m.get('context_length','?'):>7} in=${inp:.3f}/M out=${out:.3f}/M")
PY
echo

bold "3. Chat completion (non-streaming)"
REQ='{"model":"'"$MODEL"'","messages":[{"role":"user","content":"Say only the word OK."}],"max_tokens":4}'
curl -sS -w "\nHTTP %{http_code}\n" "$BASE/chat/completions" -H "$AUTH" -H "$JSON" -d "$REQ" -o "$OUTDIR/03-chat-response.json"
cat "$OUTDIR/03-chat-response.json"; echo
CHAT_ID=$(python3 -c 'import json; print(json.load(open("'"$OUTDIR"'/03-chat-response.json")).get("id",""))' 2>/dev/null || echo "")
USAGE=$(python3 -c 'import json; print(json.load(open("'"$OUTDIR"'/03-chat-response.json")).get("usage","MISSING"))' 2>/dev/null || echo "PARSE-FAIL")
echo "chat_id   = $CHAT_ID"
echo "usage     = $USAGE"

bold "4. Per-request signature (/v1/signature/{id})"
if [[ -n "$CHAT_ID" ]]; then
  curl -sS -w "\nHTTP %{http_code}\n" "$BASE/signature/$CHAT_ID?model=$MODEL" -H "$AUTH" -o "$OUTDIR/04-signature.json"
  cat "$OUTDIR/04-signature.json"; echo
else
  echo "skip — no chat_id"
fi

bold "5. Attestation report (/v1/attestation/report) with nonce"
curl -sS -w "\nHTTP %{http_code}\n" "$BASE/attestation/report?model=$MODEL&nonce=$NONCE" -H "$AUTH" -o "$OUTDIR/05-attestation.json"
preview "$OUTDIR/05-attestation.json"

bold "6. Streaming chat completion (SSE)"
STREAM_REQ='{"model":"'"$MODEL"'","messages":[{"role":"user","content":"Count: 1 2 3."}],"max_tokens":16,"stream":true}'
curl -sS "$BASE/chat/completions" -H "$AUTH" -H "$JSON" -d "$STREAM_REQ" -o "$OUTDIR/06-stream.sse"
head -c 2000 "$OUTDIR/06-stream.sse"; echo

bold "Done. Outputs saved to $OUTDIR/"
ls -la "$OUTDIR"
