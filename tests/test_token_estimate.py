"""Unit tests for _estimate_prompt_tokens — pre-flight credit-check estimate.
Must count text AND image parts. Underestimating images = pre-flight bypass."""
from main import _estimate_prompt_tokens, _IMAGE_TOK_ESTIMATE


def test_empty_messages_returns_at_least_one():
    assert _estimate_prompt_tokens({"messages": []}) >= 1


def test_simple_text_message():
    body = {"messages": [{"role": "user", "content": "hello world"}]}
    tokens = _estimate_prompt_tokens(body)
    assert tokens > 0
    assert tokens < 10  # short string


def test_multimodal_text_part_counted():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                ],
            }
        ]
    }
    tokens = _estimate_prompt_tokens(body)
    assert tokens >= 1


def test_image_url_charged_worst_case():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what"},
                    {"type": "image_url", "image_url": {"url": "https://x/y.jpg"}},
                ],
            }
        ]
    }
    tokens = _estimate_prompt_tokens(body)
    assert tokens >= _IMAGE_TOK_ESTIMATE


def test_multiple_images_stack():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://x/a.jpg"}},
                    {"type": "image_url", "image_url": {"url": "https://x/b.jpg"}},
                    {"type": "image_url", "image_url": {"url": "https://x/c.jpg"}},
                ],
            }
        ]
    }
    tokens = _estimate_prompt_tokens(body)
    assert tokens >= 3 * _IMAGE_TOK_ESTIMATE


def test_unknown_part_type_ignored():
    # Future part types shouldn't break the estimator.
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "video", "video_url": "x"},
                    {"type": "text", "text": "hello"},
                ],
            }
        ]
    }
    tokens = _estimate_prompt_tokens(body)
    assert tokens >= 1
