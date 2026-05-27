"""Unit tests for config.image_cost_micro_usd and IMAGE_MODELS plumbing.

Image billing is flat-rate per image at a given quality. The function must:
- Return integer micro-USD (never a float)
- Return 0 for unknown model
- Apply MARKUP_PROXY_NUM (since all current image models are proxy-tier)
- Multiply linearly by n
- Default to 'standard' quality when an unknown quality is passed
- Clamp n to [1, IMAGE_MAX_N]
"""
from config import (
    image_cost_micro_usd, IMAGE_MODELS, IMAGE_MAX_N, IMAGE_ALLOWED_SIZES,
    MARKUP_PROXY_NUM, MICRO,
)


def test_unknown_model_returns_zero():
    assert image_cost_micro_usd("nonexistent/model", 1) == 0
    assert image_cost_micro_usd("", 5) == 0


def test_known_dalle_standard():
    # openai/dall-e-3 standard: $0.04 wholesale * proxy markup.
    expected = round(0.04 * (MARKUP_PROXY_NUM / 100) * MICRO * 1)
    assert image_cost_micro_usd("openai/dall-e-3", 1, "standard") == expected


def test_known_dalle_hd():
    # openai/dall-e-3 hd: $0.08 wholesale * proxy markup.
    expected = round(0.08 * (MARKUP_PROXY_NUM / 100) * MICRO * 1)
    assert image_cost_micro_usd("openai/dall-e-3", 1, "hd") == expected


def test_n_scales_linearly():
    one  = image_cost_micro_usd("stability/stable-diffusion-3-5-large", 1, "standard")
    five = image_cost_micro_usd("stability/stable-diffusion-3-5-large", 5, "standard")
    assert five == one * 5


def test_unknown_quality_falls_back_to_standard():
    standard = image_cost_micro_usd("recraft/recraft-v3", 1, "standard")
    bogus    = image_cost_micro_usd("recraft/recraft-v3", 1, "ultra-mega-quality")
    assert standard == bogus


def test_n_clamped_to_max():
    capped = image_cost_micro_usd("segmind/sd3-turbo", 999, "standard")
    expected = image_cost_micro_usd("segmind/sd3-turbo", IMAGE_MAX_N, "standard")
    assert capped == expected


def test_n_clamped_to_min():
    # n=0 or negative should still produce at least one image's cost.
    bad = image_cost_micro_usd("segmind/sd3-turbo", 0, "standard")
    one = image_cost_micro_usd("segmind/sd3-turbo", 1, "standard")
    assert bad == one


def test_returns_int():
    cost = image_cost_micro_usd("stability/stable-diffusion-3-5-large", 3, "hd")
    assert isinstance(cost, int)
    assert cost > 0


def test_all_image_models_have_pricing():
    for mid, m in IMAGE_MODELS.items():
        assert "price_per_image" in m, f"{mid} missing price_per_image"
        assert "standard" in m["price_per_image"], f"{mid} missing standard price"
        cost = image_cost_micro_usd(mid, 1, "standard")
        assert cost > 0, f"{mid} standard price computed to zero"


def test_allowed_sizes_nonempty():
    assert "1024x1024" in IMAGE_ALLOWED_SIZES
    assert IMAGE_MAX_N >= 1
