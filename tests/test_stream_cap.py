"""Unit tests for the per-key concurrent-stream cap (M7).
Ensures _MAX_STREAMS_PER_KEY enforces correctly + releases properly."""
import asyncio
import pytest

from main import _stream_acquire, _stream_release, _MAX_STREAMS_PER_KEY, _active_streams


@pytest.fixture(autouse=True)
def _clear_streams():
    """Reset stream counter before each test."""
    _active_streams.clear()
    yield
    _active_streams.clear()


@pytest.mark.asyncio
async def test_acquire_under_cap_succeeds():
    for _ in range(_MAX_STREAMS_PER_KEY):
        assert await _stream_acquire("k1") is True
    assert _active_streams["k1"] == _MAX_STREAMS_PER_KEY


@pytest.mark.asyncio
async def test_acquire_at_cap_fails():
    for _ in range(_MAX_STREAMS_PER_KEY):
        await _stream_acquire("k1")
    assert await _stream_acquire("k1") is False
    assert _active_streams["k1"] == _MAX_STREAMS_PER_KEY


@pytest.mark.asyncio
async def test_release_decrements():
    await _stream_acquire("k1")
    await _stream_acquire("k1")
    assert _active_streams["k1"] == 2
    await _stream_release("k1")
    assert _active_streams["k1"] == 1


@pytest.mark.asyncio
async def test_release_to_zero_removes_key():
    await _stream_acquire("k1")
    await _stream_release("k1")
    # Counter at 0 should be removed from dict to keep it small over time
    assert "k1" not in _active_streams


@pytest.mark.asyncio
async def test_release_after_full_cycle_allows_new_acquires():
    for _ in range(_MAX_STREAMS_PER_KEY):
        await _stream_acquire("k1")
    assert await _stream_acquire("k1") is False
    await _stream_release("k1")
    assert await _stream_acquire("k1") is True


@pytest.mark.asyncio
async def test_cap_per_key_not_global():
    """Two distinct keys can each have _MAX_STREAMS_PER_KEY concurrent streams."""
    for _ in range(_MAX_STREAMS_PER_KEY):
        assert await _stream_acquire("alice") is True
    for _ in range(_MAX_STREAMS_PER_KEY):
        assert await _stream_acquire("bob") is True
    assert _active_streams["alice"] == _MAX_STREAMS_PER_KEY
    assert _active_streams["bob"] == _MAX_STREAMS_PER_KEY


@pytest.mark.asyncio
async def test_concurrent_acquires_respect_cap():
    """100 concurrent acquires for one key → exactly _MAX_STREAMS_PER_KEY succeed."""
    results = await asyncio.gather(*[_stream_acquire("k1") for _ in range(100)])
    successes = sum(1 for r in results if r)
    assert successes == _MAX_STREAMS_PER_KEY


@pytest.mark.asyncio
async def test_release_idempotent_at_zero():
    """Calling release more than acquire shouldn't go negative or crash."""
    await _stream_acquire("k1")
    await _stream_release("k1")
    await _stream_release("k1")  # extra release
    # Should be gone from dict, no exception
    assert "k1" not in _active_streams
