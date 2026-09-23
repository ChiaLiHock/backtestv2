"""The live path's tail must not change what the rule says.

`engine/live_engine.py` evaluates the rule over the newest `LIVE_TAIL_BARS` of
each timeframe rather than all of history, because a full MT5 build is ~400k bars
and ~24 s per closed bar and the loop ticks every 5 s. That is a **shortcut**, and
the only thing that makes it legitimate is that these columns converge.

`tests/test_live.py` pins the same claim for the chart's overlay columns. This
file pins it for the thing that actually decides a trade: **every leg of the
rule, and the bracket derived from it, must be bit-identical to a full-history
evaluation.** If a future change makes a leg read a path-dependent column that
does not converge (the swing-structure family), this is the test that fails.

It is deliberately a comparison and not a golden file: a golden would pin today's
answer, and what needs pinning is that the two code paths agree on ANY day.
"""

from __future__ import annotations

import pytest

from backtest.data.db import INTERVAL_MS, CandleRepository, Database
from backtest.engine.live_engine import LIVE_TAIL_BARS
from backtest.engine.live_signal import LiveSignal, RuleConfig
from backtest.tools import validate_rule as VR

PANEL = "MT5:GOLD"


def panel_cfg(**over):
    """A config pointed at PANEL, whatever the live default happens to be.

    The tail shortcut has to hold on the deepest panel available, which is the
    MT5 one — 400k bars across five timeframes is where a 1,500-bar tail is
    actually a shortcut worth checking.
    """
    from dataclasses import replace

    return replace(RuleConfig(), symbol=PANEL, bracket_mode="atr")

# Every field of an `Evaluation` that a decision or an order depends on. `ts_utc`
# is wall-clock and `atr_entry`/`close` are floats read off the same bar, so they
# are compared too — a tail that shifted the ATR would move the stop.
DECISION_FIELDS = (
    "bar_open_ms", "bar_close_ms", "fired", "would_enter", "legs", "blocking",
    "close", "atr_entry", "entry_ref", "sl", "tp", "time_stop_ms", "lots",
    "in_session", "reason_if_skipped", "config_hash",
)


def _skip_without_panel(minimum: int = 5000):
    with Database() as db:
        d = CandleRepository(db).load(PANEL, "15m")
    if len(d) < minimum:
        pytest.skip(f"needs {PANEL} 15m synced")


@pytest.fixture(scope="module")
def pair():
    """The same bar, evaluated twice: full history, then tailed."""
    _skip_without_panel()
    cfg = panel_cfg()
    with Database() as db:
        # The two builds differ only by `tail_bars`, which IS part of the cache
        # key — but clearing anyway keeps this test honest if that ever changes:
        # a hit here would compare a value with itself and pass for free.
        VR._CTX_CACHE.clear()
        full = LiveSignal(cfg).evaluate_latest(db)
        VR._CTX_CACHE.clear()
        tail = LiveSignal(cfg, tail_bars=LIVE_TAIL_BARS).evaluate_latest(db)
        VR._CTX_CACHE.clear()
    return full, tail


@pytest.mark.parametrize("field", DECISION_FIELDS)
def test_tail_matches_full_history(pair, field):
    full, tail = pair
    assert getattr(tail, field) == getattr(full, field), (
        f"{field} differs between a {LIVE_TAIL_BARS}-bar tail and full history. "
        "Either the tail is too short, or a leg now reads a column that does not "
        "converge — the swing-structure family does not."
    )


def test_every_leg_is_compared(pair):
    """A leg added later must be covered here without anyone remembering to.

    `legs` is compared as a whole dict above, so this only guards the case where
    the rule silently produces no legs at all — which would make the comparison
    vacuously true.
    """
    full, _ = pair
    assert len(full.legs) >= 6


def test_backfill_ignores_the_instance_tail():
    """A tailed instance must still walk the whole panel when backfilling.

    `backfill` seeds the audit log and drives the §D3 reconciliation. If it
    inherited the live tail, the reconciliation would compare a truncated panel
    against a full one and the disagreement would look like a rule bug.
    """
    _skip_without_panel()
    import time

    since = int((time.time() - 20 * 86400) * 1000)
    with Database() as db:
        VR._CTX_CACHE.clear()
        wide = LiveSignal(panel_cfg()).backfill(db, since_ms=since)
        VR._CTX_CACHE.clear()
        tailed = LiveSignal(panel_cfg(), tail_bars=200).backfill(db, since_ms=since)
        VR._CTX_CACHE.clear()
    # Compared over the bars BOTH runs saw. A watcher syncing this panel while
    # the suite runs can append a bar between the two backfills, and then the
    # lengths differ for a reason that has nothing to do with the tail. The
    # claim being tested is that the tail changes no VERDICT, so the overlap is
    # what has to agree — and it has to be a real overlap, not two empty lists.
    a = {e.bar_open_ms: e for e in wide}
    b = {e.bar_open_ms: e for e in tailed}
    shared = sorted(set(a) & set(b))
    assert len(shared) > 500, f"only {len(shared)} shared bars — window too small"
    assert [a[k].fired for k in shared] == [b[k].fired for k in shared]
    assert [a[k].would_enter for k in shared] == [b[k].would_enter for k in shared]
    assert [a[k].legs for k in shared] == [b[k].legs for k in shared]


def test_default_is_full_history():
    """The tail must be opt-in. A measurement that silently tailed would be wrong."""
    assert LiveSignal(panel_cfg()).tail_bars is None
    assert VR.build_context.__defaults__[-1] is None


# ---------------------------------------------------------------------------
# The context cache must be keyed on the DATA, not on the connection object
# ---------------------------------------------------------------------------


# These assert on IDENTITY, not on `len(_CTX_CACHE)`. The cache is module-global
# and bounded, so any other test in the run can populate or evict it — a size
# assertion passes or fails depending on what else ran, which is exactly the kind
# of flake that gets a suite ignored. "Did this call return the same object?"
# answers the real question and cannot be perturbed from outside.


def test_context_cache_survives_a_new_connection():
    """Two Database objects over the same data must share the cached context.

    Keying on `id(db)` made this a miss, which is the harmless half of that bug —
    it only wasted 18 seconds rebuilding something identical.
    """
    _skip_without_panel()
    from backtest.indicators.base import IndicatorConfig

    cfg = IndicatorConfig()
    with Database() as db:
        first = VR.build_context(db, PANEL, "15m", cfg, 3)
    with Database() as db2:
        second = VR.build_context(db2, PANEL, "15m", cfg, 3)
    assert second[0] is first[0], "a second connection re-built an identical context"


def test_context_cache_misses_when_a_new_bar_arrives():
    """The half of the `id(db)` bug that could serve STALE BARS to a live loop.

    A process holding one Database open across a bar close would keep hitting a
    context built before that bar existed, and `evaluate_latest` would answer
    about the bar before last — forever, and silently. The key carries the newest
    stored bar, so new data cannot hit.

    Simulated by re-keying a real entry as if it had been built one bar ago: the
    lookup must then miss and return a DIFFERENT object.
    """
    _skip_without_panel()
    from backtest.indicators.base import IndicatorConfig

    cfg = IndicatorConfig()
    with Database() as db:
        first = VR.build_context(db, PANEL, "15m", cfg, 3)
        key = next(k for k, v in VR._CTX_CACHE.items() if v is first)
        stale = list(key)
        bar_pos = next(i for i, v in enumerate(stale)
                       if isinstance(v, int) and v > 10 ** 12)
        stale[bar_pos] -= INTERVAL_MS["15m"]
        VR._CTX_CACHE[tuple(stale)] = VR._CTX_CACHE.pop(key)
        again = VR.build_context(db, PANEL, "15m", cfg, 3)
    assert again[0] is not first[0], "a stale-bar entry was served as a hit"


def test_the_newest_bar_is_part_of_the_key():
    """Stated directly, so the property survives a refactor of the key's shape."""
    _skip_without_panel()
    from backtest.indicators.base import IndicatorConfig

    with Database() as db:
        newest = VR._newest_bar(db, PANEL, "15m")
        VR.build_context(db, PANEL, "15m", IndicatorConfig(), 3)
        assert any(newest in k for k in VR._CTX_CACHE)


def test_context_cache_is_bounded():
    """It holds five computed timeframes per entry; unbounded is hundreds of MB."""
    assert VR._CTX_CACHE_MAX <= 16
