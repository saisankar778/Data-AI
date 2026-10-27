"""List prices in USD per million tokens (input, output), from the Anthropic model table of
2026-09-25. Cache reads bill at 0.1x input and cache writes at 1.25x input."""

from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def cost_usd(
    model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0
) -> float:
    price = PRICES.get(model)
    if price is None:
        return 0.0
    inp, outp = price
    return (
        input_tokens * inp
        + output_tokens * outp
        + cache_read * inp * 0.1
        + cache_write * inp * 1.25
    ) / 1_000_000
