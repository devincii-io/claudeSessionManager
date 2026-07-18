"""Model pricing table and cost computation.

Prices are USD per 1,000,000 tokens. Cache economics follow Anthropic's public
model: cache **reads** cost ~0.1x the base input rate, 5-minute cache **writes**
~1.25x, and 1-hour writes ~2x. Values are editable — the UI treats these as a
starting point a user can override.
"""

from __future__ import annotations

from dataclasses import dataclass

CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.00


@dataclass(frozen=True)
class ModelPrice:
    input: float  # USD per 1M input tokens
    output: float  # USD per 1M output tokens
    context_window: int  # max input tokens


# Keyed by exact model id. Unknown models fall back to a mid-tier estimate.
PRICES: dict[str, ModelPrice] = {
    "claude-fable-5": ModelPrice(10.0, 50.0, 1_000_000),
    "claude-mythos-5": ModelPrice(10.0, 50.0, 1_000_000),
    "claude-opus-4-8": ModelPrice(5.0, 25.0, 1_000_000),
    "claude-opus-4-7": ModelPrice(5.0, 25.0, 1_000_000),
    "claude-opus-4-6": ModelPrice(5.0, 25.0, 1_000_000),
    "claude-opus-4-5": ModelPrice(5.0, 25.0, 1_000_000),
    "claude-opus-4-1": ModelPrice(15.0, 75.0, 200_000),
    "claude-opus-4-0": ModelPrice(15.0, 75.0, 200_000),
    "claude-3-opus": ModelPrice(15.0, 75.0, 200_000),
    "claude-sonnet-5": ModelPrice(3.0, 15.0, 1_000_000),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0, 1_000_000),
    "claude-sonnet-4-5": ModelPrice(3.0, 15.0, 1_000_000),
    "claude-sonnet-4-0": ModelPrice(3.0, 15.0, 200_000),
    "claude-3-7-sonnet": ModelPrice(3.0, 15.0, 200_000),
    "claude-3-5-sonnet": ModelPrice(3.0, 15.0, 200_000),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0, 200_000),
    "claude-3-5-haiku": ModelPrice(0.80, 4.0, 200_000),
    "claude-3-haiku": ModelPrice(0.25, 1.25, 200_000),
}

_FALLBACK = ModelPrice(5.0, 25.0, 1_000_000)


def price_for(model: str | None) -> ModelPrice:
    if not model:
        return _FALLBACK
    if model in PRICES:
        return PRICES[model]
    # Tolerate date-suffixed ids like "claude-haiku-4-5-20251001".
    for key, price in PRICES.items():
        if model.startswith(key):
            return price
    return _FALLBACK


def context_window(model: str | None) -> int:
    return price_for(model).context_window


@dataclass
class Usage:
    """Aggregated token counts, summable across messages."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0  # treated as 5-minute writes for costing

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


def cost_for(usage: Usage, model: str | None) -> float:
    """Return the estimated USD cost of a usage bundle for a given model."""
    p = price_for(model)
    return (
        usage.input * p.input
        + usage.output * p.output
        + usage.cache_read * p.input * CACHE_READ_MULTIPLIER
        + usage.cache_write * p.input * CACHE_WRITE_5M_MULTIPLIER
    ) / 1_000_000.0
