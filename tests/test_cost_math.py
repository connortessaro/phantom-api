"""Unit tests for config.cost_micro_usd. All money in integer micro-USD.

The function must:
- Never return a float
- Never return a negative value
- Round (not truncate) the final markup-applied cost
- Match a hand-computed reference for known model rates
- Apply MARKUP_NUM/100 uniformly
"""
from config import cost_micro_usd, MARKUP_NUM, MODELS


def test_zero_tokens_zero_cost():
    for mid in MODELS:
        assert cost_micro_usd(mid, 0, 0) == 0


def test_returns_int_not_float():
    cost = cost_micro_usd("phala/qwen-2.5-7b-instruct", 1000, 1000)
    assert isinstance(cost, int)


def test_known_model_reference():
    # phala/qwen-2.5-7b-instruct: input $0.04/M, output $0.10/M.
    # For 1M prompt + 1M completion: raw = 1M*0.04 + 1M*0.10 = 0.14M micro-USD wholesale.
    # Marked up by MARKUP_NUM/100 (the TEE-tier rate for phala/* models).
    cost = cost_micro_usd("phala/qwen-2.5-7b-instruct", 1_000_000, 1_000_000)
    expected = round((1_000_000 * 0.04 + 1_000_000 * 0.10) * MARKUP_NUM / 100)
    assert cost == expected


def test_small_token_cost_rounds_correctly():
    # 1 prompt token + 1 completion token on cheap model.
    # Raw micro-USD: 1*0.04 + 1*0.10 = 0.14
    # Marked: 0.14 * 1.15 = 0.161 → round to 0
    cost = cost_micro_usd("phala/qwen-2.5-7b-instruct", 1, 1)
    assert cost == 0  # tiny costs round to zero — acceptable


def test_expensive_model_charges_more_than_cheap():
    p, c = 1_000_000, 1_000_000
    cheap = cost_micro_usd("phala/qwen-2.5-7b-instruct", p, c)
    expensive = cost_micro_usd("phala/kimi-k2.6", p, c)
    assert expensive > cheap


def test_markup_applied():
    # Cost MUST be greater than raw wholesale by exactly markup factor.
    m = MODELS["phala/gpt-oss-120b"]
    p_tok, c_tok = 1_000_000, 1_000_000
    raw_micro = p_tok * m["input_per_m"] + c_tok * m["output_per_m"]
    expected = round(raw_micro * MARKUP_NUM / 100)
    actual = cost_micro_usd("phala/gpt-oss-120b", p_tok, c_tok)
    assert actual == expected
    if MARKUP_NUM > 100:
        assert actual > raw_micro


def test_completion_more_expensive_than_prompt():
    # Output tokens cost more than input tokens in all models with that gap.
    p_only = cost_micro_usd("phala/glm-5.1", 1_000_000, 0)
    c_only = cost_micro_usd("phala/glm-5.1", 0, 1_000_000)
    assert c_only > p_only  # output_per_m=4.20 > input_per_m=1.21


def test_huge_token_count_no_overflow():
    # 1B prompt + 1B completion — must not overflow int math.
    cost = cost_micro_usd("phala/kimi-k2.6", 1_000_000_000, 1_000_000_000)
    assert cost > 0
    assert isinstance(cost, int)


def test_cost_monotonic_in_tokens():
    # Adding tokens should never reduce cost.
    mid = "phala/glm-4.7-flash"
    prev = cost_micro_usd(mid, 0, 0)
    for n in (1, 10, 100, 1000, 10_000, 100_000):
        cur = cost_micro_usd(mid, n, n)
        assert cur >= prev
        prev = cur
