"""Model pricing. Source of truth is LiteLLM's public price list, cached for 7 days.

Falls back to a bundled snapshot so the tool works offline. Override any model
with ~/.tokenaudit/prices_override.json:  {"my-model": {"in": 1e-6, "out": 4e-6}}
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from functools import lru_cache
from pathlib import Path

from .store import HOME

LITELLM_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
               "model_prices_and_context_window.json")
CACHE = HOME / "prices.json"
OVERRIDE = HOME / "prices_override.json"
FALLBACK = Path(__file__).with_name("prices_fallback.json")
MAX_AGE = 7 * 86400


def refresh(timeout: int = 20) -> int:
    """Download the latest LiteLLM price list. Returns number of models cached."""
    with urllib.request.urlopen(LITELLM_URL, timeout=timeout) as r:
        raw = json.load(r)
    models = {}
    for k, v in raw.items():
        if not isinstance(v, dict) or not v.get("input_cost_per_token"):
            continue
        models[k] = {"in": v["input_cost_per_token"], "out": v.get("output_cost_per_token") or 0,
                     "cr": v.get("cache_read_input_token_cost"),
                     "cw": v.get("cache_creation_input_token_cost"),
                     "p": v.get("litellm_provider")}
    HOME.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"_fetched": time.strftime("%Y-%m-%d"), "models": models}))
    _table.cache_clear()
    return len(models)


@lru_cache(maxsize=1)
def _table() -> dict:
    models = json.loads(FALLBACK.read_text())["models"]
    if CACHE.exists():
        try:
            models.update(json.loads(CACHE.read_text())["models"])
        except Exception:
            pass
    if OVERRIDE.exists():
        models.update(json.loads(OVERRIDE.read_text()))
    return models


def maybe_refresh() -> None:
    """Refresh quietly if the cache is missing or stale. Never raises."""
    try:
        if not CACHE.exists() or time.time() - CACHE.stat().st_mtime > MAX_AGE:
            refresh(timeout=8)
    except Exception:
        pass


_DATE = re.compile(r"(-\d{8}|-\d{4}-\d{2}-\d{2}|@\d{8}|-v\d+:\d+|-latest)$")


def _normalize(model: str) -> list[str]:
    m = (model or "").strip().lower()
    m = m.split("/")[-1]                      # strip provider prefixes
    m = re.sub(r"^models/", "", m)
    cands = [m]
    base = m
    while True:
        nxt = _DATE.sub("", base)
        if nxt == base:
            break
        base = nxt
        cands.append(base)
    # claude-3-5-sonnet style vs claude-sonnet-3-5 both exist in the wild; try both
    return cands


@lru_cache(maxsize=4096)
def lookup(model: str) -> dict | None:
    t = _table()
    for c in _normalize(model):
        if c in t:
            return t[c]
    # longest known key that the model name starts with
    best = None
    for c in _normalize(model):
        for k in t:
            if c.startswith(k) and (best is None or len(k) > len(best)):
                best = k
    return t[best] if best else None


def cost(model: str, input: int = 0, output: int = 0, cache_read: int = 0,
         cache_write: int = 0) -> float | None:
    p = lookup(model)
    if not p:
        return None
    cr = p.get("cr") if p.get("cr") is not None else p["in"]
    cw = p.get("cw") if p.get("cw") is not None else p["in"]
    return input * p["in"] + output * p["out"] + cache_read * cr + cache_write * cw


# Cheaper sibling for "was a smaller model enough?" checks.
DOWNGRADE = [
    (r"claude.*(opus|mythos|fable)", "claude-sonnet-4-5"),
    (r"claude.*sonnet", "claude-haiku-4-5"),
    (r"gpt-5(\.\d+)?(-codex)?(-max|-pro)?$", "gpt-5-mini"),
    (r"gpt-4o$|gpt-4\.1$|gpt-4-turbo", "gpt-4o-mini"),
    (r"^o\d", "gpt-5-mini"),
    (r"gemini.*pro", "gemini-2.5-flash"),
]


def cheaper_alternative(model: str) -> str | None:
    m = (model or "").lower().split("/")[-1]
    for pat, alt in DOWNGRADE:
        if re.search(pat, m) and alt not in m:
            if lookup(alt):
                return alt
    return None
