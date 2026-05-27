"""Unit tests for streaming usage extraction. Critical: a bug here = free inference exploit."""
from main import _extract_usage


def test_simple_usage_block():
    s = '... "usage":{"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59}'
    p, c = _extract_usage(s)
    assert p == 42
    assert c == 17


def test_usage_with_whitespace_variations():
    s = '"prompt_tokens"  :  100 ,  "completion_tokens"  :  50'
    p, c = _extract_usage(s)
    assert p == 100
    assert c == 50


def test_no_usage_returns_zero():
    s = 'data: {"id": "chatcmpl-xyz", "choices": [...]}'
    p, c = _extract_usage(s)
    assert p == 0 and c == 0


def test_takes_largest_when_multiple():
    # If multiple usage entries appear (re-tries?) take the largest.
    s = '"prompt_tokens":10,"completion_tokens":5 ... "prompt_tokens":50,"completion_tokens":30'
    p, c = _extract_usage(s)
    assert p == 50
    assert c == 30


def test_split_across_buffer_lines():
    # SSE format with newlines between data: blocks. Usage block sits across multiple lines.
    s = (
        'data: {"id":"chatcmpl-x","choices":[],"usage":{'
        '"prompt_tokens": 38,\n'
        '"total_tokens": 50,\n'
        '"completion_tokens": 12\n'
        '}}\n\n'
    )
    p, c = _extract_usage(s)
    assert p == 38
    assert c == 12


def test_huge_token_counts():
    s = '"prompt_tokens":999999999,"completion_tokens":888888888'
    p, c = _extract_usage(s)
    assert p == 999_999_999
    assert c == 888_888_888


def test_empty_string():
    p, c = _extract_usage("")
    assert p == 0 and c == 0
