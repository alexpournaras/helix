"""Cost from a price table (USD per 1M tokens). Defaults are list prices from PLAN C3/C28; the user's
table (`data/research_prices.json`, same shape) overrides them. Unknown models cost `None`, not 0."""

from __future__ import annotations

import json
from pathlib import Path

# input, output, cached_input (cached input defaults to 10% of input when not listed)
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "gpt-6.2-astra": {"input": 8.0, "output": 40.0},
    "gpt-6.2-sol": {"input": 2.0, "output": 10.0},
    "claude-fable-5-2": {"input": 8.0, "output": 40.0},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0},
    "fake-a": {"input": 0.0, "output": 0.0},
    "fake-b": {"input": 0.0, "output": 0.0},
}


class PriceTable:
    def __init__(self, path: Path | None = None, prices: dict | None = None):
        self.prices = {k: dict(v) for k, v in DEFAULT_PRICES.items()}
        if path is not None:
            try:
                data = json.loads(Path(path).read_text("utf-8"))
                if isinstance(data, dict):
                    for model, entry in data.items():
                        if isinstance(entry, dict):
                            self.prices[model] = {k: float(v) for k, v in entry.items()}
            except (OSError, ValueError):
                pass
        if prices:
            self.prices.update(prices)

    def cost(self, model: str, input_tokens: int | None, output_tokens: int | None,
             cached_input_tokens: int | None = 0) -> float | None:
        entry = self.prices.get(model)
        if entry is None or input_tokens is None or output_tokens is None:
            return None
        cached = min(cached_input_tokens or 0, input_tokens)
        cached_price = entry.get("cached_input", entry["input"] * 0.1)
        total = ((input_tokens - cached) * entry["input"] + cached * cached_price + output_tokens * entry["output"])
        return round(total / 1_000_000, 6)
