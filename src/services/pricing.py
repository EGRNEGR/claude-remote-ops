"""Claude model prices (USD per million tokens), copied from the model catalog baked into Claude Code 2.1.304.

The CLI computes `costUSD` with exactly these tiers (`pricing_tiers` + `models[].pricing` in claude.exe), so
costs recomputed here from `message.usage` match the `cost-state` totals it writes. Cache writes are priced
by TTL: `usage.cache_creation.ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens`. A tier with
`long_prompt` switches to other prices once the prompt (input + cache read + cache write) is above the
threshold. On a claude.ai subscription these are API-equivalent amounts, not money actually charged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Price:
    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    long_prompt: "Price | None" = None
    long_prompt_above: int = 0


def _tier(i: float, o: float, w5: float, w1: float, r: float, **kw: object) -> Price:
    return Price(i, o, w5, w1, r, **kw)  # type: ignore[arg-type]


TIERS: dict[str, Price] = {
    "tier_2_10": _tier(2, 10, 2.5, 4, 0.2),
    "tier_2_10_cache_read_0_10": _tier(2, 10, 2.5, 4, 0.1),
    "tier_3_15": _tier(3, 15, 3.75, 6, 0.3),
    "tier_5_25": _tier(5, 25, 6.25, 10, 0.5),
    "tier_15_75": _tier(15, 75, 18.75, 30, 1.5),
    "tier_10_50": _tier(10, 50, 12.5, 20, 1),
    "tier_10_50_cache_read_0_25": _tier(10, 50, 12.5, 20, 0.25),
    "tier_4_20_cache_read_0_20": _tier(4, 20, 5, 8, 0.2),
    "haiku_35": _tier(0.8, 4, 1, 1.6, 0.08),
    "haiku_45": _tier(1, 5, 1.25, 2, 0.1),
    "haiku_55": _tier(0.1, 0.5, 0.125, 0.2, 0.01,
                      long_prompt=_tier(0.5, 2.5, 0.625, 1, 0.05), long_prompt_above=100_000),
}

MODEL_TIERS: dict[str, str] = {
    "claude-3-5-haiku": "haiku_35",
    "claude-haiku-4-5": "haiku_45",
    "claude-haiku-5-5": "haiku_55",
    "claude-3-5-sonnet": "tier_3_15",
    "claude-3-7-sonnet": "tier_3_15",
    "claude-sonnet-4-0": "tier_3_15",
    "claude-sonnet-4": "tier_3_15",
    "claude-sonnet-4-5": "tier_3_15",
    "claude-sonnet-4-6": "tier_3_15",
    "claude-sonnet-5": "tier_2_10",
    "claude-sonnet-5-5": "tier_2_10_cache_read_0_10",
    "claude-opus-4-0": "tier_15_75",
    "claude-opus-4": "tier_15_75",
    "claude-opus-4-1": "tier_15_75",
    "claude-opus-4-5": "tier_5_25",
    "claude-opus-4-6": "tier_5_25",
    "claude-opus-4-7": "tier_5_25",
    "claude-opus-4-8": "tier_5_25",
    "claude-opus-5": "tier_5_25",
    "claude-opus-5-5": "tier_4_20_cache_read_0_20",
    "claude-fable-5": "tier_10_50",
    "claude-fable-5-1": "tier_10_50_cache_read_0_25",
    "claude-mythos-5": "tier_10_50",
    "claude-mythos-5-1": "tier_10_50_cache_read_0_25",
}
# Longest id first, so "claude-opus-5-5-20261001" maps to claude-opus-5-5 and not to claude-opus-5.
_PREFIXES = sorted(MODEL_TIERS, key=len, reverse=True)
_DATE_SUFFIX_RE = re.compile(r"[-@]\d{8}$")
_PROVIDER_PREFIX_RE = re.compile(r"^(?:[a-z]{2}\.)?(?:anthropic[./])")
SYNTHETIC_MODEL = "<synthetic>"  # CLI-generated messages (errors, interrupts): no API call, no cost


def normalize_model_id(model: str | None) -> str:
    """`claude-fable-5.1` / `us.anthropic.claude-opus-5-5-20261001-v1:0` / `claude-opus-5-5[1m]` -> canonical id."""
    text = (model or "").strip().lower()
    if not text or text == SYNTHETIC_MODEL:
        return text
    text = _PROVIDER_PREFIX_RE.sub("", text)
    text = re.sub(r"\[[^\]]*\]$", "", text)
    text = re.sub(r"-v\d+(?::\d+)?$", "", text)
    text = _DATE_SUFFIX_RE.sub("", text)
    return text.replace(".", "-")


def price_for(model: str | None) -> Price | None:
    canonical = normalize_model_id(model)
    if not canonical or canonical == SYNTHETIC_MODEL:
        return None
    tier = MODEL_TIERS.get(canonical)
    if tier is None:
        tier = next((MODEL_TIERS[p] for p in _PREFIXES if canonical.startswith(p + "-")), None)
    return TIERS.get(tier) if tier else None


def request_cost(
    model: str | None, input_tokens: int, output_tokens: int, cache_read: int, cache_write_5m: int,
    cache_write_1h: int,
) -> float | None:
    """USD for one API request. 0.0 for synthetic messages, None for a model missing from the catalog."""
    if normalize_model_id(model) in ("", SYNTHETIC_MODEL):
        return 0.0
    price = price_for(model)
    if price is None:
        return None
    prompt = input_tokens + cache_read + cache_write_5m + cache_write_1h
    if price.long_prompt is not None and prompt > price.long_prompt_above:
        price = price.long_prompt
    return (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read * price.cache_read
        + cache_write_5m * price.cache_write_5m
        + cache_write_1h * price.cache_write_1h
    ) / 1_000_000
