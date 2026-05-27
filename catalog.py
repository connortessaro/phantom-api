"""Dynamic model catalog. Fetches Redpill's /v1/models on startup, refreshes
hourly in the background, classifies each model into tier ("tee" or "proxy"),
applies phantom's per-tier markup, and exposes the lookup helpers used by
main.py + /v1/models response.

Why dynamic:
- Redpill catalog churns; hard-coding 80 entries is unmaintainable.
- Tier classification is derived from Redpill's `providers` field, not phantom guesswork.
- Pricing changes upstream propagate automatically.

Fallback chain: live HTTP → on-disk cache → built-in minimum (qwen-2.5-7b only,
so the service still starts even if everything else fails).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from decimal import Decimal
from typing import Any

import httpx

from config import (
    REDPILL_API_BASE, REDPILL_API_KEY,
    MARKUP_TEE_NUM, MARKUP_PROXY_NUM,
    MICRO, CATALOG_CACHE_PATH, CATALOG_REFRESH_SECONDS,
    CATALOG_BLOCKLIST, IMAGE_MODELS,
)

logger = logging.getLogger("phantom.catalog")

# Provider classification. Lowercase + de-punctuated for matching.
# Per Redpill docs (verified via firecrawl 2026-05): the following providers run
# their inference inside Intel TDX + NVIDIA H100 CC GPU TEEs and produce a
# verifiable attestation. Everything else is a "proxied" tier — the gateway
# itself runs in TDX but the model runs on the vendor's normal infrastructure.
_TEE_PROVIDERS = {"phala", "tinfoil", "chutes", "nearai", "near"}
_PROXY_PROVIDERS = {"openai", "anthropic", "google", "xai", "deepseek", "meta", "metallama", "moonshotai"}


def _norm_provider(p: str) -> str:
    return p.lower().replace("-", "").replace(" ", "").replace("_", "")


def _classify_tier(model: dict[str, Any]) -> str:
    """Return 'tee' or 'proxy' based on Redpill's `providers` field, falling
    back to model-id prefix when providers is empty/unknown."""
    providers = {_norm_provider(p) for p in model.get("providers") or []}
    if providers & _TEE_PROVIDERS:
        return "tee"
    if providers & _PROXY_PROVIDERS:
        # Models served via proxy-only paths (openai/anthropic/google/xai) are
        # tier-2 even if they're also offered TEE somewhere else.
        # Exception: if the model is ALSO marked phala-prefix, prefer tee.
        if model.get("id", "").startswith("phala/"):
            return "tee"
        return "proxy"
    # No providers metadata. Use prefix.
    mid = model.get("id", "")
    if mid.startswith("phala/"):
        return "tee"
    return "proxy"


def _is_embedding(model: dict[str, Any]) -> bool:
    """Embedding models return vectors, not text. Detect by output_modalities
    (most reliable) or by id-substring fallback."""
    output_mods = {m.lower() for m in model.get("output_modalities") or []}
    if "embedding" in output_mods or "embeddings" in output_mods:
        return True
    mid = model.get("id", "").lower()
    return "embedding" in mid or mid.startswith("sentence-transformers/")


def _per_m_from_per_token(price_per_token: str | float | None) -> float:
    """Redpill returns prompt/completion price as $/token strings like
    "0.00000125". Convert to $/M tokens (multiply by 1M)."""
    if price_per_token is None:
        return 0.0
    try:
        return float(Decimal(str(price_per_token)) * Decimal(1_000_000))
    except Exception:
        return 0.0


def _build_meta(model: dict[str, Any]) -> dict[str, Any] | None:
    """Convert Redpill's model entry into phantom's internal metadata shape.
    Returns None if model should be dropped (blocklisted, broken pricing, etc.)."""
    mid = model.get("id")
    if not mid or mid in CATALOG_BLOCKLIST:
        return None

    pricing = model.get("pricing") or {}
    input_per_m = _per_m_from_per_token(pricing.get("prompt"))
    output_per_m = _per_m_from_per_token(pricing.get("completion"))

    # Drop models with broken pricing (negative, zero on a chat model, etc.)
    is_emb = _is_embedding(model)
    if not is_emb and input_per_m <= 0:
        return None

    tier = _classify_tier(model)
    return {
        "id": mid,
        "tier": tier,                       # "tee" or "proxy"
        "kind": "embedding" if is_emb else "chat",
        "description": (model.get("description") or model.get("name") or "")[:280],
        "context": int(model.get("context_length") or 0) or 32_768,
        "input_per_m": input_per_m,        # $/M tokens, wholesale (pre-markup)
        "output_per_m": output_per_m,
        "providers": model.get("providers") or [],
        "input_modalities": model.get("input_modalities") or ["text"],
    }


def _markup_factor(tier: str) -> float:
    return (MARKUP_TEE_NUM if tier == "tee" else MARKUP_PROXY_NUM) / 100.0


def cost_micro_usd(meta: dict[str, Any], prompt_tokens: int, completion_tokens: int) -> int:
    """Marked-up cost in micro-USD for a given chat/embedding model meta.
    Tier-aware: TEE models use MARKUP_TEE_NUM, proxy models use MARKUP_PROXY_NUM.
    For image models this is not used — see config.image_cost_micro_usd."""
    factor = _markup_factor(meta["tier"])
    raw = prompt_tokens * meta["input_per_m"] + completion_tokens * meta["output_per_m"]
    return int(raw * factor + 0.5)


def _image_meta_for_catalog(mid: str, m: dict[str, Any]) -> dict[str, Any]:
    """Project IMAGE_MODELS row into the same schema used by /v1/models so
    image models appear alongside chat/embedding entries (kind='image').
    input_per_m / output_per_m are zeroed — image billing is per-image."""
    return {
        "id": mid,
        "tier": m["tier"],
        "kind": "image",
        "description": m["description"],
        "context": 0,                                 # not meaningful for images
        "input_per_m": 0.0,
        "output_per_m": 0.0,
        "providers": [mid.split("/", 1)[0]],
        "input_modalities": ["text"],
        "max_size": m.get("max_size"),
        "price_per_image": m.get("price_per_image", {}),
    }


# ─── In-memory catalog state ──────────────────────────────────────────────────

_state: dict[str, Any] = {
    "loaded_at": 0,
    "source": "none",                 # "live" | "disk" | "builtin"
    "models": {},                     # phantom id -> meta dict (post-rebrand)
    "aliases": {},                    # upstream/legacy id -> phantom id
}


# Provider priority for TEE-rebrand dedup. Lower index = preferred when
# multiple upstream IDs collapse to the same phantom/<base>. phala/ wins
# because its attestation provenance is the most direct; everything else
# is a same-model duplicate Redpill re-exposes under the original author's
# prefix.
_PROVIDER_PRIORITY = (
    "phala",
    "qwen", "deepseek", "moonshotai", "z-ai", "google", "openai",
    "meta-llama", "minimax", "nousresearch", "tinfoil", "chutes",
    "near", "nearai",
)


def _provider_rank(upstream_id: str) -> int:
    head = upstream_id.split("/", 1)[0].lower() if "/" in upstream_id else ""
    head = head.replace("_", "").replace(" ", "")
    try:
        return _PROVIDER_PRIORITY.index(head)
    except ValueError:
        return 999


def _phantom_base(upstream_id: str) -> str:
    """Strip provider prefix. phala/kimi-k2.6 → kimi-k2.6."""
    return upstream_id.split("/", 1)[1] if "/" in upstream_id else upstream_id


def _rebrand(models: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Rewrite TEE chat + embedding model IDs to phantom/<base>. PROXY chat
    and image models keep their vendor prefix so customers can tell at a
    glance that their prompt content is visible to the vendor (anthropic/*,
    openai/*, stability/*, etc.).

    Dedup: when multiple upstream IDs collapse to the same phantom/<base>
    (e.g., phala/kimi-k2.6 + moonshotai/kimi-k2.6), pick the one with the
    best provider rank. Losers still get recorded in `aliases` so legacy
    client IDs resolve to the canonical phantom id.

    Returns (rebranded_models, aliases). aliases maps every known upstream
    id (and any legacy phantom id from a prior rebrand snapshot) to the
    canonical phantom id, enabling backwards-compat ID resolution."""
    out: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}

    for upstream_id, meta in models.items():
        meta = dict(meta)
        # Preserve the original Redpill id so forwarders can translate back.
        meta.setdefault("upstream_id", upstream_id)
        actual_upstream = meta["upstream_id"]

        tier = meta.get("tier", "tee")
        kind = meta.get("kind", "chat")

        # Only rewrite TEE chat + embedding. Images and PROXY chat keep
        # vendor prefix.
        if tier != "tee" or kind == "image":
            out[upstream_id] = meta
            aliases[upstream_id] = upstream_id
            continue

        phantom_id = f"phantom/{_phantom_base(actual_upstream)}"
        prev = out.get(phantom_id)
        if prev is None or _provider_rank(actual_upstream) < _provider_rank(prev["upstream_id"]):
            meta["id"] = phantom_id
            out[phantom_id] = meta
        # Either branch: alias the upstream id (and the legacy phantom id
        # this entry may already carry on disk-cache reload) to the chosen
        # phantom_id so old clients still resolve.
        aliases[actual_upstream] = phantom_id
        if upstream_id != actual_upstream:
            aliases[upstream_id] = phantom_id

    return out, aliases


def resolve_upstream_id(model_id: str) -> str | None:
    """Translate a phantom-branded or legacy id to the upstream Redpill id.
    Used at the boundary before forwarding chat/embeddings/attest requests."""
    m = get(model_id)
    return m.get("upstream_id") if m else None


def _builtin_fallback() -> dict[str, dict[str, Any]]:
    """Last-resort catalog if both live HTTP and disk cache fail. Keeps service
    minimally alive — just the cheapest Phala model so phantom can still serve."""
    return {
        "phala/qwen-2.5-7b-instruct": {
            "id": "phala/qwen-2.5-7b-instruct",
            "tier": "tee",
            "kind": "chat",
            "description": "Qwen 2.5 7B (builtin fallback — catalog refresh failed)",
            "context": 32_768,
            "input_per_m": 0.04,
            "output_per_m": 0.10,
            "providers": ["phala"],
            "input_modalities": ["text"],
        }
    }


def _save_to_disk(models: dict[str, dict[str, Any]]) -> None:
    try:
        d = os.path.dirname(CATALOG_CACHE_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = CATALOG_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"saved_at": int(time.time()), "models": models}, f, indent=2)
        os.replace(tmp, CATALOG_CACHE_PATH)
    except Exception as e:
        logger.warning(f"catalog: failed to write cache: {type(e).__name__}: {e}")


def _load_from_disk() -> dict[str, dict[str, Any]] | None:
    try:
        with open(CATALOG_CACHE_PATH) as f:
            data = json.load(f)
        models = data.get("models")
        if isinstance(models, dict) and models:
            return models
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f"catalog: failed to read cache: {type(e).__name__}: {e}")
    return None


# Redpill's /v1/models doesn't list embedding models (they're accessible via
# /v1/embeddings but not advertised in the catalog feed). Hardcode them here so
# they appear in phantom's catalog. Pricing matches Redpill's published rates.
_STATIC_EMBEDDINGS = {
    "qwen/qwen3-embedding-8b": {
        "id": "qwen/qwen3-embedding-8b",
        "tier": "tee",
        "kind": "embedding",
        "description": "Qwen3 Embedding 8B. multilingual, 32K context, 4096-d vectors.",
        "context": 32_768,
        "input_per_m": 0.01,
        "output_per_m": 0.0,
        "providers": ["phala"],
        "input_modalities": ["text"],
    },
    "sentence-transformers/all-minilm-l6-v2": {
        "id": "sentence-transformers/all-minilm-l6-v2",
        "tier": "tee",
        "kind": "embedding",
        "description": "all-MiniLM-L6-v2. tiny + fast, 384-d vectors, 512 ctx.",
        "context": 512,
        "input_per_m": 0.005,
        "output_per_m": 0.0,
        "providers": ["phala"],
        "input_modalities": ["text"],
    },
}


async def _fetch_live(timeout: float = 15.0) -> dict[str, dict[str, Any]] | None:
    if not REDPILL_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(
                f"{REDPILL_API_BASE}/models",
                headers={"Authorization": f"Bearer {REDPILL_API_KEY}"},
            )
        if r.status_code != 200:
            logger.warning(f"catalog: live fetch HTTP {r.status_code}")
            return None
        raw = r.json().get("data") or []
    except Exception as e:
        logger.warning(f"catalog: live fetch failed: {type(e).__name__}: {e}")
        return None

    out = {}
    for entry in raw:
        meta = _build_meta(entry)
        if meta:
            out[meta["id"]] = meta
    # Always merge in embedding models — Redpill doesn't list them in /v1/models.
    for mid, meta in _STATIC_EMBEDDINGS.items():
        if mid not in CATALOG_BLOCKLIST:
            out[mid] = dict(meta)
    # Image models — Redpill catalog also omits these. Phantom curates them
    # statically with flat-rate pricing (see config.IMAGE_MODELS).
    for mid, meta in IMAGE_MODELS.items():
        if mid not in CATALOG_BLOCKLIST:
            out[mid] = _image_meta_for_catalog(mid, meta)
    return out if out else None


def _ensure_image_models(models: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Image models live in config.IMAGE_MODELS — always merge them into the
    catalog regardless of source (live / disk / builtin) since Redpill's
    /v1/models doesn't list them and disk cache might be older than the curated
    list."""
    for mid, meta in IMAGE_MODELS.items():
        if mid in CATALOG_BLOCKLIST:
            continue
        if mid not in models or models[mid].get("kind") != "image":
            models[mid] = _image_meta_for_catalog(mid, meta)
    return models


def _commit(models: dict[str, dict[str, Any]], source: str) -> None:
    """Apply rebrand + image-merge in one shot, then publish to _state.
    Order matters: image merge must run on UPSTREAM ids (PROXY tier, vendor
    prefix preserved), then rebrand only touches TEE chat + embedding."""
    models = _ensure_image_models(models)
    rebranded, aliases = _rebrand(models)
    _state["models"] = rebranded
    _state["aliases"] = aliases
    _state["loaded_at"] = int(time.time())
    _state["source"] = source


async def refresh() -> None:
    """Pull a fresh catalog from Redpill. Falls back to disk cache then builtin.
    Updates module-level state on success."""
    live = await _fetch_live()
    if live:
        _commit(live, "live")
        _save_to_disk(_state["models"])
        logger.info(f"catalog: loaded {len(_state['models'])} models from live Redpill")
        return

    disk = _load_from_disk()
    if disk:
        _commit(disk, "disk")
        logger.warning(f"catalog: live fetch failed, using {len(_state['models'])} models from disk cache")
        return

    _commit(_builtin_fallback(), "builtin")
    logger.error("catalog: live + disk both failed, using minimum builtin fallback")


async def _refresh_loop():
    """Background task: refresh every CATALOG_REFRESH_SECONDS."""
    while True:
        try:
            await asyncio.sleep(CATALOG_REFRESH_SECONDS)
            await refresh()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"catalog: refresh loop error: {type(e).__name__}: {e}")


def start_background_refresh() -> asyncio.Task:
    return asyncio.create_task(_refresh_loop())


# ─── Public API ───────────────────────────────────────────────────────────────

def all_models() -> dict[str, dict[str, Any]]:
    return _state["models"]


def get(model_id: str) -> dict[str, Any] | None:
    """Look up a model by phantom-branded id (`phantom/<base>`), vendor-prefix
    id (`anthropic/...`, `openai/...`), or any legacy/upstream id we've seen
    on prior refreshes. Returns None if unknown."""
    m = _state["models"].get(model_id)
    if m is not None:
        return m
    canonical = _state.get("aliases", {}).get(model_id)
    if canonical:
        return _state["models"].get(canonical)
    return None


def allowed_ids() -> set[str]:
    return set(_state["models"].keys())


def chat_ids() -> set[str]:
    return {mid for mid, m in _state["models"].items() if m["kind"] == "chat"}


def embedding_ids() -> set[str]:
    return {mid for mid, m in _state["models"].items() if m["kind"] == "embedding"}


def image_ids() -> set[str]:
    return {mid for mid, m in _state["models"].items() if m["kind"] == "image"}


def info() -> dict[str, Any]:
    """Debug info — never expose publicly."""
    return {
        "loaded_at": _state["loaded_at"],
        "source": _state["source"],
        "count": len(_state["models"]),
    }
