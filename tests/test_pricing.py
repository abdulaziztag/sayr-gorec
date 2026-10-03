from gorets.digest.pricing import cost_usd, price_for


def test_prefix_match_and_fallback() -> None:
    assert price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)
    assert price_for("claude-sonnet-5-5") == (2.0, 10.0)
    assert price_for("claude-opus-5-5") == (4.0, 20.0)
    assert price_for("claude-unknown-9") == (2.0, 10.0)


def test_cost_with_batch_and_cache() -> None:
    plain = cost_usd("claude-haiku-4-5-20251001", input_tokens=1_000_000, output_tokens=100_000)
    assert plain == 1.5
    batch = cost_usd(
        "claude-haiku-4-5-20251001", input_tokens=1_000_000, output_tokens=100_000, batch=True
    )
    assert batch == 0.75
    cached = cost_usd(
        "claude-sonnet-5-5",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert cached == 2.0 * 0.1 + 2.0 * 1.25
