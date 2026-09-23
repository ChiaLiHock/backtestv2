"""Docs-vs-code drift guards.

`SIGNAL_MAP.md` is handed to people (and to other sessions) as the vocabulary they
can write configs against. It silently fell 26 signals behind the engine once
already, which would send a reader to write a config that raises `KeyError`.
These tests make that failure loud instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.engine.signals import REGISTRY
from backtest.indicators.registry import compute_indicators

DOCS = Path(__file__).resolve().parents[1] / "docs"
SIGNAL_MAP = DOCS / "SIGNAL_MAP.md"


def _doc() -> str:
    return SIGNAL_MAP.read_text(encoding="utf-8")


def test_every_registered_signal_is_documented():
    """The engine must never gain a signal the map does not mention."""
    doc = _doc()
    missing = sorted(
        name for name in REGISTRY
        if not re.search(r"\b" + re.escape(name) + r"\b", doc)
    )
    assert not missing, (
        f"{len(missing)} signal_id(s) exist in the engine but not in SIGNAL_MAP.md: "
        f"{missing}. Regenerate the status header at the top of that file."
    )


def test_implemented_list_matches_the_registry_exactly():
    """The '✅ Implemented' block is the contract — it must be neither short nor long."""
    doc = _doc()
    start = doc.find("## ✅ Implemented")
    assert start > 0, "SIGNAL_MAP.md is missing its Implemented section"
    block = doc[start : doc.find("## ⛔", start)]
    listed = set(re.findall(r"\b([a-z][a-z0-9_]{3,})\b", block)) & set(REGISTRY)

    assert listed == set(REGISTRY), (
        "Implemented list is out of sync. "
        f"missing={sorted(set(REGISTRY) - listed)} "
        f"stale={sorted(listed - set(REGISTRY))}"
    )


def test_not_implemented_names_really_are_absent():
    """Nothing in the ⛔ block may actually exist — otherwise the warning is wrong
    and a reader is told to avoid something that works."""
    doc = _doc()
    start = doc.find("## ⛔ Proposed")
    assert start > 0, "SIGNAL_MAP.md is missing its not-implemented section"
    block = doc[start : doc.find("**Why each family is missing:**", start)]
    claimed = set(re.findall(r"\b([a-z][a-z0-9_]{3,})\b", block))
    wrong = sorted(claimed & set(REGISTRY))
    assert not wrong, (
        f"listed as NOT implemented but present in the registry: {wrong}"
    )


def test_expr_columns_exist_on_a_real_frame():
    """Every column the doc offers for `expr:` must actually be produced."""
    n = 300
    df = pd.DataFrame({
        "open_time": np.arange(n, dtype="int64") * 3_600_000,
        "open": np.full(n, 1.0), "high": np.full(n, 1.1),
        "low": np.full(n, 0.9), "close": np.full(n, 1.0),
        "volume": np.full(n, 1.0),
    })
    produced = set(compute_indicators(df, "1h").columns) | {"close", "high", "low", "open"}

    doc = _doc()
    start = doc.find("## Columns usable in an `expr:` condition")
    assert start > 0
    block = doc[start : doc.find("---", start)]
    listed = set(re.findall(r"\b([a-z][a-z0-9_]{3,})\b", block))
    # only judge names that look like columns, not prose
    candidates = {x for x in listed if x in produced or x.count("_") >= 1}
    missing = sorted(x for x in candidates if x not in produced and x not in {"expr", "condition", "ema_28"})
    assert not missing, f"documented expr columns that do not exist: {missing}"


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_signal_evaluates_without_error(name: str):
    """A registered signal must actually run and return a bool array of the right
    length. Catches typos in column names that would otherwise only surface when
    someone writes a config using it."""
    from backtest.engine.signals import SignalContext, evaluate_signal

    n = 400
    rng = np.random.default_rng(4)
    close = 2000 + np.cumsum(rng.normal(0, 2.0, n))
    df = pd.DataFrame({
        "open_time": np.arange(n, dtype="int64") * 3_600_000,
        "open": close, "high": close + 2, "low": close - 2, "close": close,
        "volume": np.abs(rng.normal(100, 20, n)),
    })
    ind = compute_indicators(df, "1h")
    htf = {"1h": ind, "4h": ind}
    ctx = SignalContext(
        ind=ind, close=close, high=df["high"].to_numpy(), low=df["low"].to_numpy(),
        open_time=df["open_time"].to_numpy(),
        thresholds={"ut_near_atr": 0.6, "zone_near_atr": 0.5,
                    "dynamic_near_atr": 0.5, "zone_touch_atr": 0.25},
        htf=htf,
    )
    out = evaluate_signal(name, ctx)
    assert out.dtype == bool, f"{name} did not return booleans"
    assert out.size == n, f"{name} returned {out.size} values for {n} bars"
