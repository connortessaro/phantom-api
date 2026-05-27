"""Phantom API configuration. Loaded from .env via python-dotenv.
Secrets stay in os.environ for the lifetime of the process. Read access to
/proc/$pid/environ is restricted to root and the phantom user via standard
Unix permissions, which is the defense in depth we rely on. (Previous attempt
to pop env vars post-capture broke uvicorn --reload due to module re-import.)"""
import os
from dotenv import load_dotenv

load_dotenv()

# Upstream gateway. Phala Network's product is Redpill (api.redpill.ai). We
# keep `phala/*` as the model-ID prefix because that signals "this model runs
# in a Phala TEE enclave" — a capability tag, not a brand. The env-var rename
# from PHALA_* to REDPILL_* matches the actual product we're calling.
# Legacy PHALA_API_KEY / PHALA_API_BASE still accepted as fallback for old .env files.
REDPILL_API_BASE = os.environ.get("REDPILL_API_BASE") or os.environ.get("PHALA_API_BASE", "https://api.redpill.ai/v1")
REDPILL_API_KEY = os.environ.get("REDPILL_API_KEY") or os.environ.get("PHALA_API_KEY")
if not REDPILL_API_KEY:
    raise KeyError("REDPILL_API_KEY (or legacy PHALA_API_KEY) must be set")

# ─── Payment rails ────────────────────────────────────────────────────────────
# PAYMENT_PROVIDER controls which rail(s) handle new purchases:
#   "legacy_xmr"  - operator-PC wallet over Tor (oldest path, still works)
#   "nowpayments" - NowPayments PSP only (multi-crypto, KYC operator)
#   "monero_pay"  - MoneroPay daemon only (XMR direct, zero PSP)
#   "hybrid"      - MoneroPay (XMR direct) + NowPayments (other coins).
#                   Frontend picks rail via {"rail":"xmr"} or {"rail":"multi"} body field.
PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "legacy_xmr")

# NowPayments (multi-crypto PSP). Required for nowpayments + hybrid modes.
NP_API_KEY        = os.environ.get("NP_API_KEY", "")
NP_IPN_SECRET     = os.environ.get("NP_IPN_SECRET", "")
NP_BASE           = os.environ.get("NP_BASE", "https://api.nowpayments.io/v1")
NP_PAYOUT_CURRENCY = os.environ.get("NP_PAYOUT_CURRENCY", "xmr")

# MoneroPay (self-hosted XMR daemon). Required for monero_pay + hybrid modes.
# Daemon runs alongside monero-wallet-rpc on the operator PC. VPS reaches it
# over Tor (set MONEROPAY_URL to http://<onion>:5000) or locally for dev.
#
# MoneroPay does NOT sign callbacks. Phantom authenticates them via per-
# payment URL path tokens: callback_url is "{PUBLIC_BASE_URL}/v1/monero-pay/
# callback/{payment_id}" where payment_id is a 16-byte token_urlsafe. The
# daemon doesn't know payment_id ahead of time — it just echoes the URL
# back. Triple-bind on receive: URL token == body description == DB row.
MONEROPAY_URL     = os.environ.get("MONEROPAY_URL", "http://127.0.0.1:5000")
MONEROPAY_USE_TOR = MONEROPAY_URL.startswith("http://") and ".onion" in MONEROPAY_URL

# Multi-crypto rail markup. Applied on top of bundle/custom price when the
# customer pays via NowPayments (not XMR direct). Covers:
#  - NowPayments service fee (~1%)
#  - Payout auto-conversion to XMR (~0.5%)
#  - Crypto rate volatility within 10-min fixed-rate lock (~0.5%)
#  - Refund / failed-payment buffer + convenience premium
# Default 5: customer pays 1.05× sticker price; credit issued is unchanged.
# Set to 0 to absorb fees fully (operator margin hit).
MULTI_CRYPTO_SURCHARGE_PERCENT = int(os.environ.get("MULTI_CRYPTO_SURCHARGE_PERCENT", "5"))

# Public base URL phantom advertises (used for IPN callbacks + redirect URLs
# in NowPayments invoices). Must be reachable from the public internet.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://api.phantom.codes")

if PAYMENT_PROVIDER in ("nowpayments", "hybrid"):
    if not NP_API_KEY or not NP_IPN_SECRET:
        raise KeyError(
            "NP_API_KEY and NP_IPN_SECRET must be set when "
            f"PAYMENT_PROVIDER={PAYMENT_PROVIDER}"
        )
if PAYMENT_PROVIDER in ("monero_pay", "hybrid"):
    if not MONEROPAY_URL:
        raise KeyError(
            "MONEROPAY_URL must be set when "
            f"PAYMENT_PROVIDER={PAYMENT_PROVIDER}"
        )

# Wallet RPC location. Defaults to localhost (hot wallet on VPS).
# Set WALLET_RPC_HOST to an .onion to switch to operator-PC-via-Tor mode.
WALLET_RPC_HOST = os.environ.get("WALLET_RPC_HOST", "127.0.0.1")
WALLET_RPC_PORT = int(os.environ.get("WALLET_RPC_PORT", "18083"))
WALLET_RPC_URL = f"http://{WALLET_RPC_HOST}:{WALLET_RPC_PORT}/json_rpc"
WALLET_USE_TOR = WALLET_RPC_HOST.endswith(".onion")
WALLET_RPC_USER = os.environ.get("WALLET_RPC_USER", "phantom")
# Required only when PAYMENT_PROVIDER=legacy_xmr (the self-hosted wallet rail).
# Under nowpayments, no wallet RPC is reached at all; leave it empty.
WALLET_RPC_PASSWORD = os.environ.get("WALLET_RPC_PASSWORD", "")
if PAYMENT_PROVIDER == "legacy_xmr" and not WALLET_RPC_PASSWORD:
    raise KeyError("WALLET_RPC_PASSWORD must be set when PAYMENT_PROVIDER=legacy_xmr")
TOR_SOCKS_URL = os.environ.get("TOR_SOCKS_URL", "socks5://127.0.0.1:9050")

# Hot/cold sweep config. COLD_ADDRESS receives funds from hot wallet when balance
# crosses HOT_SWEEP_THRESHOLD_USD (computed against live XMR/USD price). Run via
# cron: scripts/sweep-hot.sh. HOT_SWEEP_THRESHOLD_XMR is a hard floor used as
# fallback if price fetch fails.
COLD_ADDRESS = os.environ.get("COLD_ADDRESS", "")
HOT_SWEEP_THRESHOLD_USD = os.environ.get("HOT_SWEEP_THRESHOLD_USD", "30")
HOT_SWEEP_THRESHOLD_XMR = os.environ.get("HOT_SWEEP_THRESHOLD_XMR", "0.08363058")

PAYMENT_EXPIRY_MINUTES = 60

DB_PATH = os.environ.get("DB_PATH", "data/phantom.db")

# Maximum total outstanding customer credit allowed across all active keys.
# Operator should set this conservatively below current Redpill balance so we never
# sell credit we can't honor. Defaults to $1000 for dev. override in production.
# Legacy PHALA_BUDGET_USD still accepted for backwards compat with old .env files.
REDPILL_BUDGET_MICRO = int(float(
    os.environ.get("REDPILL_BUDGET_USD") or os.environ.get("PHALA_BUDGET_USD", "1000")
) * 1_000_000)

# Custom-amount purchases: min/max bounds in micro-USD.
# Min prevents subaddress-spam attack; max limits AML profile + single-buyer concentration.
CUSTOM_MIN_MICRO = int(float(os.environ.get("CUSTOM_MIN_USD", "1")) * 1_000_000)
CUSTOM_MAX_MICRO = int(float(os.environ.get("CUSTOM_MAX_USD", "1000")) * 1_000_000)
CUSTOM_VALIDITY_DAYS = int(os.environ.get("CUSTOM_VALIDITY_DAYS", "90"))

# Dynamic catalog config (see catalog.py). On-disk cache survives restarts so
# phantom serves even if Redpill is unreachable on boot.
CATALOG_CACHE_PATH = os.environ.get("CATALOG_CACHE_PATH", "/opt/phantom-api/data/catalog-cache.json")
CATALOG_REFRESH_SECONDS = int(os.environ.get("CATALOG_REFRESH_SECONDS", "3600"))
# Models to never expose, even if Redpill ships them. Add IDs here to suppress.
# Defaults suppress UPSTREAM duplicates that map to the same phantom/<base>
# after rebrand (e.g., moonshotai/kimi-k2.6 + phala/kimi-k2.6 + kimi-k2.6 all
# collapse to phantom/kimi-k2.6 — keep one, drop the rest so the catalog isn't
# cluttered with synonyms). Customers can still resolve any legacy id via the
# alias table in catalog.py.
_DEFAULT_BLOCKLIST = {
    # Kept canonical: phala/kimi-k2.5, phala/kimi-k2.6 (rebrand to phantom/*)
    "moonshotai/kimi-k2.5",
    "moonshotai/kimi-k2.6",
    # Kept canonical: phala/deepseek-*
    "deepseek/deepseek-chat-v3.1",
    "deepseek/deepseek-v3.2",
    "deepseek/deepseek-v4-pro",
    # Kept canonical: phala/gemma-*
    "google/gemma-3-27b-it",
    "google/gemma-4-31b-it",
    # Kept canonical: phala/glm-*
    "z-ai/glm-4.7",
    "z-ai/glm-4.7-flash",
    "z-ai/glm-5",
    "z-ai/glm-5.1",
    # Kept canonical: phala/gpt-oss-*
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    # Kept canonical: phala/qwen-*, phala/qwen3-*, phala/qwen3.5-*, phala/qwen3.6-*
    "qwen/qwen-2.5-7b-instruct",
    "qwen/qwen3-30b-a3b-instruct-2507",
    "qwen/qwen3-vl-30b-a3b-instruct",
    "qwen/qwen3.5-27b",
    "qwen/qwen3.5-397b-a17b",
    "qwen/qwen3.6-35b-a3b",
    # qwen/qwen3-32b and qwen/qwen3.5-122b-a10b have NO phala equivalent — keep
    # both visible so customers still see them; they rebrand to phantom/<base>.
    # Kept canonical: phala/minimax-m2.5
    "minimax/minimax-m2.5",
    # Llama: only one upstream, leave alone
}
CATALOG_BLOCKLIST = _DEFAULT_BLOCKLIST | set(filter(None, (
    s.strip() for s in os.environ.get("CATALOG_BLOCKLIST", "").split(",")
)))

# Per-tier markup over upstream wholesale rate.
# Tier 1 ("tee"): phala/*, tinfoil/*, chutes/*, near-ai/* — full TEE attestation.
#   Customer's anon premium over Phala-direct (where Phala-direct is +5% over wholesale).
# Tier 2 ("proxy"): openai/*, anthropic/*, google/*, xai/* — gateway TDX only,
#   model itself runs on vendor infra and sees prompt content. Higher markup because
#   (a) operator KYC-burden with each vendor, (b) anon access has fewer substitutes,
#   (c) Phantom carries TOS-risk on suspended-account scenarios.
#
# Legacy MARKUP_PERCENT still accepted; applied to both tiers if the new vars are unset.
_LEGACY_MARKUP = int(os.environ.get("MARKUP_PERCENT", "30"))
MARKUP_TEE_PERCENT   = int(os.environ.get("MARKUP_TEE_PERCENT",   str(_LEGACY_MARKUP)))
MARKUP_PROXY_PERCENT = int(os.environ.get("MARKUP_PROXY_PERCENT", str(max(_LEGACY_MARKUP, 50))))
MARKUP_TEE_NUM   = 100 + MARKUP_TEE_PERCENT     # used as fraction /100
MARKUP_PROXY_NUM = 100 + MARKUP_PROXY_PERCENT
# Kept for legacy import sites; resolves to TEE rate (default tier).
MARKUP_PERCENT = MARKUP_TEE_PERCENT
MARKUP_NUM = MARKUP_TEE_NUM

# Internal currency unit: micro-USD. $1 = 1_000_000 micro-USD.
MICRO = 1_000_000

# USD-credit bundles. price_usd is what user pays; credit_usd is what they get to spend.
# Volume bonus pushes bigger upfront purchases (less wallet ops overhead).
# All money values are integer micro-USD (1 USD = 1_000_000 micro-USD). Never use floats for money.
BUNDLES = {
    "test":   {"price_micro":      50_000, "credit_micro":      50_000, "validity_days": 1,   "confirmations":  1},   # stagenet smoke test only
    "small":  {"price_micro":  10_000_000, "credit_micro":  10_000_000, "validity_days": 90,  "confirmations":  2},
    "medium": {"price_micro":  50_000_000, "credit_micro":  55_000_000, "validity_days": 90,  "confirmations":  4},
    "large":  {"price_micro": 200_000_000, "credit_micro": 230_000_000, "validity_days": 180, "confirmations":  6},
    "whale":  {"price_micro": 500_000_000, "credit_micro": 600_000_000, "validity_days": 365, "confirmations": 10},
}


def confirmations_for_credit(credit_micro: int) -> int:
    """Tier confirmations by credit size (USD). Block ≈ 2 min, so confs ≈ wait min / 2.
    Smaller buys clear faster; large buys take the full 10-conf finality window."""
    usd = credit_micro / MICRO
    if usd < 5:    return 1
    if usd < 50:   return 2
    if usd < 200:  return 4
    if usd < 500:  return 6
    return 10


def confirmations_for_payment(bundle_name: str, credit_micro: int) -> int:
    """Resolve confs from stored payment row. Bundle wins if known; else fall
    back to credit-size tier (covers 'custom' purchases + unknown bundles)."""
    b = BUNDLES.get(bundle_name)
    if b and "confirmations" in b:
        return int(b["confirmations"])
    return confirmations_for_credit(credit_micro)

# All TEE-attested via Phala (Intel TDX + NVIDIA CC) and routed through Redpill.
# Pricing = USD per 1M tokens at upstream wholesale. User-facing price = these × MARKUP_NUM/100.
# Curated catalog. Add models only after confirming TEE attestation works via phase-0-smoke.sh.
# kind: chat | embedding (affects which endpoint accepts the model + cost shape).
MODELS = {
    # ─── Chat models ───────────────────────────────────────────────────────────

    # Cheap fast chat — dev testing, simple Qs
    "phala/qwen-2.5-7b-instruct":       {"tier": "tee", "kind": "chat", "description": "Qwen 2.5 7B. cheapest tier, fast simple chat",               "context":  32_768, "input_per_m": 0.04,  "output_per_m": 0.10},

    # Smaller MoE — cheapest tier 2, drop-in for 120b when latency matters
    "phala/gpt-oss-20b":                {"tier": "tee", "kind": "chat", "description": "OpenAI GPT-OSS 20B. small fast MoE",                        "context": 131_072, "input_per_m": 0.04,  "output_per_m": 0.15},

    # Default balanced — best $/quality ratio
    "phala/gpt-oss-120b":               {"tier": "tee", "kind": "chat", "description": "OpenAI GPT-OSS 120B. balanced reasoning MoE, default pick", "context": 131_072, "input_per_m": 0.10,  "output_per_m": 0.49},

    # Budget premium — large Qwen3.5
    "phala/qwen3.5-27b":                {"tier": "tee", "kind": "chat", "description": "Qwen 3.5 27B. budget premium tier, large context",          "context": 262_144, "input_per_m": 0.30,  "output_per_m": 2.40},

    # Massive open-weight — biggest in TEE
    "phala/qwen3.5-397b-a17b":          {"tier": "tee", "kind": "chat", "description": "Qwen 3.5 397B (17B active MoE). largest TEE open-weight",   "context": 262_144, "input_per_m": 0.70,  "output_per_m": 2.80},

    # DeepSeek V3 in TEE
    "phala/deepseek-v3.2":              {"tier": "tee", "kind": "chat", "description": "DeepSeek V3.2. flagship open-weight reasoning + code",      "context": 131_072, "input_per_m": 0.27,  "output_per_m": 1.10},
    "phala/deepseek-chat-v3.1":         {"tier": "tee", "kind": "chat", "description": "DeepSeek Chat V3.1. balanced reasoning",                    "context": 131_072, "input_per_m": 0.20,  "output_per_m": 0.85},

    # Google's open-weight chat + vision
    "phala/gemma-3-27b-it":             {"tier": "tee", "kind": "chat", "description": "Google Gemma 3 27B. vision + multilingual",                 "context":  53_000, "input_per_m": 0.11,  "output_per_m": 0.40},

    # Top reasoning — premium tier
    "phala/glm-5.1":                    {"tier": "tee", "kind": "chat", "description": "Z.AI GLM 5.1. premium reasoning + coding",                  "context": 202_752, "input_per_m": 1.21,  "output_per_m": 4.20},
    "phala/glm-5":                      {"tier": "tee", "kind": "chat", "description": "Z.AI GLM 5. confidential systems engineering",              "context": 202_752, "input_per_m": 0.95,  "output_per_m": 3.20},
    "phala/glm-4.7":                    {"tier": "tee", "kind": "chat", "description": "Z.AI GLM 4.7. agentic coding + tool use",                   "context": 202_752, "input_per_m": 0.50,  "output_per_m": 2.10},

    # Best coder — agentic + tool calling
    "phala/kimi-k2.6":                  {"tier": "tee", "kind": "chat", "description": "MoonshotAI Kimi K2.6. long-horizon coding, multi-agent",    "context": 262_144, "input_per_m": 1.09,  "output_per_m": 4.60},
    "phala/kimi-k2.5":                  {"tier": "tee", "kind": "chat", "description": "MoonshotAI Kimi K2.5. prior-gen coding agent",              "context": 262_144, "input_per_m": 0.90,  "output_per_m": 3.80},

    # Coding-specialist sparse-MoE
    "phala/qwen3-coder-next":           {"tier": "tee", "kind": "chat", "description": "Qwen3 Coder Next sparse-MoE 80B. agentic edits",            "context": 262_144, "input_per_m": 0.60,  "output_per_m": 2.40},

    # Vision — Qwen multimodal
    "phala/qwen3-vl-30b-a3b-instruct":  {"tier": "tee", "kind": "chat", "description": "Qwen3 VL 30B-A3B. images, GUI, video frames",               "context": 128_000, "input_per_m": 0.20,  "output_per_m": 0.70},
    "phala/qwen2.5-vl-72b-instruct":    {"tier": "tee", "kind": "chat", "description": "Qwen 2.5 VL 72B. premium vision + multimodal",              "context": 131_072, "input_per_m": 0.80,  "output_per_m": 2.40},

    # Uncensored — NSFW / no refusals
    "phala/uncensored-24b":             {"tier": "tee", "kind": "chat", "description": "Venice Uncensored 24B. no refusals, no content filter",     "context":  32_768, "input_per_m": 0.20,  "output_per_m": 0.90},
    "phala/qwen3.6-35b-a3b-uncensored": {"tier": "tee", "kind": "chat", "description": "Qwen 3.6 35B uncensored MoE. newer, larger uncensored",     "context": 131_072, "input_per_m": 0.30,  "output_per_m": 1.20},
    "phala/gemma-4-26b-a4b-uncensored": {"tier": "tee", "kind": "chat", "description": "Gemma 4 26B uncensored. Google-base, jailbreak-free",       "context":  53_000, "input_per_m": 0.25,  "output_per_m": 1.00},

    # Long context — cheap big-doc workflows
    "phala/glm-4.7-flash":              {"tier": "tee", "kind": "chat", "description": "Z.AI GLM 4.7 Flash. 202K context, fast agentic coding",     "context": 202_752, "input_per_m": 0.10,  "output_per_m": 0.43},

    # ─── Embedding models ─────────────────────────────────────────────────────
    # Embeddings return vectors, no completion tokens. output_per_m = 0.
    # Use against /v1/embeddings endpoint (not /v1/chat/completions).

    # Cheap quality — multilingual semantic search
    "qwen/qwen3-embedding-8b":          {"tier": "tee", "kind": "embedding", "description": "Qwen3 Embedding 8B. multilingual, 32K context",        "context":  32_768, "input_per_m": 0.01,  "output_per_m": 0.0},

    # Smallest cheapest — short-text similarity
    "sentence-transformers/all-minilm-l6-v2": {"tier": "tee", "kind": "embedding", "description": "all-MiniLM-L6-v2. tiny + fast, 384-d vectors",   "context":     512, "input_per_m": 0.005, "output_per_m": 0.0},
}

# ─── Image generation models ──────────────────────────────────────────────────
# Pricing = USD per generated image at given quality (flat-rate, not per-token).
# All current image models are vendor-served (Stability, Recraft, OpenAI, Segmind);
# none run in GPU TEE today. tier="proxy" — Redpill gateway hides identity, vendor
# sees the prompt. Markup uses MARKUP_PROXY_NUM. Add a TEE-attested image model
# here with tier="tee" only after phase-0-smoke confirms attestation.
#
# Schema mirrors MODELS dict — kind="image", price_per_image keyed by quality.
# Sizes accepted by upstream vary per model; phantom validates the union.
IMAGE_MODELS = {
    "stability/stable-diffusion-3-5-large":   {"tier": "proxy", "kind": "image", "max_size": "2048x2048", "description": "Stability SD 3.5 Large. high-fidelity photoreal.",         "price_per_image": {"standard": 0.04, "hd": 0.08}},
    "stability/stable-diffusion-3-5-medium":  {"tier": "proxy", "kind": "image", "max_size": "1536x1536", "description": "Stability SD 3.5 Medium. faster, lower cost.",            "price_per_image": {"standard": 0.03, "hd": 0.06}},
    "stability/stable-diffusion-ultra":       {"tier": "proxy", "kind": "image", "max_size": "2048x2048", "description": "Stability SD Ultra. premium quality.",                    "price_per_image": {"standard": 0.08, "hd": 0.12}},
    "recraft/recraft-v3":                     {"tier": "proxy", "kind": "image", "max_size": "2048x2048", "description": "Recraft v3. brand-consistent raster.",                    "price_per_image": {"standard": 0.04, "hd": 0.06}},
    "recraft/recraft-v3-svg":                 {"tier": "proxy", "kind": "image", "max_size": "2048x2048", "description": "Recraft v3 SVG. vector output for logos.",                "price_per_image": {"standard": 0.05, "hd": 0.05}},
    "openai/dall-e-3":                        {"tier": "proxy", "kind": "image", "max_size": "1024x1024", "description": "OpenAI DALL-E 3.",                                        "price_per_image": {"standard": 0.04, "hd": 0.08}},
    "segmind/sd3-turbo":                      {"tier": "proxy", "kind": "image", "max_size": "1024x1024", "description": "Segmind SD3 Turbo. fastest, cheapest.",                   "price_per_image": {"standard": 0.01, "hd": 0.02}},
}

# Sizes accepted from clients. Anything outside this set → 400.
IMAGE_ALLOWED_SIZES = {"256x256", "512x512", "1024x1024", "1536x1536", "2048x2048"}
IMAGE_MAX_N = 10  # upstream cap; phantom mirrors

ALLOWED_MODELS = set(MODELS.keys()) | set(IMAGE_MODELS.keys())
CHAT_MODELS = {k for k, v in MODELS.items() if v.get("kind", "chat") == "chat"}
EMBEDDING_MODELS = {k for k, v in MODELS.items() if v.get("kind") == "embedding"}
IMAGE_MODEL_IDS = set(IMAGE_MODELS.keys())


def image_cost_micro_usd(model_id: str, n: int, quality: str = "standard") -> int:
    """Pre-flight + actual cost for image generation. Flat rate per image,
    multiplied by markup factor for the model's tier. Returns 0 if model
    unknown (caller should reject with 400 before reaching here)."""
    m = IMAGE_MODELS.get(model_id)
    if not m:
        return 0
    table = m["price_per_image"]
    base_usd = table.get(quality, table.get("standard", 0))
    tier = m.get("tier", "proxy")
    factor = (MARKUP_PROXY_NUM if tier == "proxy" else MARKUP_TEE_NUM) / 100
    n = max(1, min(int(n), IMAGE_MAX_N))
    return int(base_usd * factor * MICRO * n + 0.5)


def cost_micro_usd(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    """Marked-up cost in micro-USD. Tier-aware: tier=tee uses MARKUP_TEE_NUM,
    tier=proxy uses MARKUP_PROXY_NUM. Dispatches to live catalog first (covers
    models added dynamically from Redpill that aren't in the static fallback dict).
    Unit derivation: input_per_m is $/1M tokens; for N tokens that's N * input_per_m
    micro-USD (the 1e6 from USD->micro cancels the 1M)."""
    # Try live catalog first (dynamic, broader, accurate up-to-date pricing).
    try:
        import catalog  # local import to avoid circular dependency at module load
        meta = catalog.get(model)
        if meta:
            return catalog.cost_micro_usd(meta, prompt_tokens, completion_tokens)
    except (ImportError, AttributeError):
        pass
    # Fallback to static MODELS dict (builtin, used by tests + first-start before refresh).
    m = MODELS[model]
    tier = m.get("tier", "tee")
    factor = (MARKUP_PROXY_NUM if tier == "proxy" else MARKUP_TEE_NUM) / 100
    raw_micro = prompt_tokens * m["input_per_m"] + completion_tokens * m["output_per_m"]
    marked = raw_micro * factor
    return int(marked + 0.5)
