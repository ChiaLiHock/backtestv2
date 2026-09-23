"""The broker server's clock, as a timezone rather than a number.

MetaTrader hands back every timestamp — bars, deals, ticks — on the **server's
wall clock**, encoded as though it were UTC. Nothing in the API or the exports
says which zone that is, so it has to be supplied.

The first version of this project measured it once, in August, got **UTC+3**, and
used that integer everywhere. That is wrong for four months of every year.

XM Global runs **EET/EEST** — UTC+2 in winter, UTC+3 in summer, switching on the
EU rule (last Sunday of March, last Sunday of October). A fixed +3 therefore
stamps every bar between late October and late March **one hour too early**.
Verified two independent ways before this module was written:

* Cross-checking `MT5:GOLD` against Bybit `XAUUSDT` at matching UTC timestamps,
  the bar-to-bar scatter (MAD) was **1.14 after 29 March 2026 and 10.72 before
  it** — and shifting the winter bars forward one hour collapsed it to 2.13.
* The stored H4 opens were `[1,5,9,13,17,21]` UTC in **both** July and January.
  A server on a UTC+3 day produces those hours; a server on UTC+2 produces
  `[2,6,10,14,18,22]`. Identical hours in both seasons is the signature of a
  conversion that ignored the transition.

**Why the whole hour matters more than it sounds.** The 4H UT Dynamic Level is a
path-dependent latching trailing stop, so a bar partition shifted by an hour is
not a slightly different number — it is a different flip history, and the 4H UT
bias is the rule's most load-bearing leg.

**Ambiguity is asserted, not resolved.** The autumn fold repeats 03:00–04:00
local and the spring gap deletes it. Both fall on a Sunday, when `GOLD` is shut,
so no bar should ever land there — and `ambiguous="raise"` / `nonexistent="raise"`
turn that expectation into a check instead of a silent guess. A continuously
quoted symbol like `GOLD24-7` genuinely does have bars in the fold, and it will
raise here rather than quietly picking one of the two possible instants.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

# XM Global MT5 9. Any EET/EEST broker shares this; a broker on a different zone
# needs this changed, and the cross-checks in tests/test_mt5_rates.py will fail
# loudly if it is wrong.
BROKER_TZ = "Europe/Athens"


class AmbiguousBrokerTime(ValueError):
    """A timestamp fell in a DST fold or gap, so it has no single UTC meaning."""


def server_naive_to_utc_ms(server_epoch_s, tz: str = BROKER_TZ) -> np.ndarray:
    """MT5 timestamps -> true UTC epoch milliseconds.

    ``server_epoch_s`` is what MT5 returns: seconds that decode to the server's
    wall clock when read as UTC. They are re-read as local time in ``tz`` and
    converted properly.
    """
    naive = pd.to_datetime(np.asarray(server_epoch_s, dtype="int64"), unit="s")
    try:
        local = naive.tz_localize(tz, ambiguous="raise", nonexistent="raise")
    except Exception as exc:                     # pytz/pandas raise several types
        raise AmbiguousBrokerTime(
            f"a timestamp falls in the {tz} DST fold or gap, so it has no single "
            f"UTC meaning: {exc}. `GOLD` is shut on the Sunday this happens, so "
            "this should not occur; a continuously-quoted symbol will hit it and "
            "needs an explicit rule for which side of the fold a bar belongs to."
        ) from None
    # Cast to an explicit millisecond dtype before taking the integers. `.view`
    # and `.asi8` return whatever resolution pandas chose for the index — here
    # `datetime64[s]`, not the nanoseconds one might assume — so dividing by a
    # hardcoded factor silently produced 1970 timestamps.
    return (local.tz_convert("UTC")
                 .tz_localize(None)
                 .values.astype("datetime64[ms]").astype("int64"))


def utc_ms_to_server_naive(ms: int, tz: str = BROKER_TZ) -> datetime:
    """True UTC epoch ms -> the naive server-clock datetime the API expects.

    The inverse of the above, for the request side: `copy_rates_range` and
    `history_deals_get` both take server wall-clock datetimes with no zone.
    """
    return (pd.Timestamp(int(ms), unit="ms", tz="UTC")
            .tz_convert(tz).tz_localize(None).to_pydatetime())


def offset_hours_at(ms: int, tz: str = BROKER_TZ) -> int:
    """The server's UTC offset at one instant, in whole hours.

    For reporting only. Nothing converts with this — that is the bug this module
    exists to remove — but it is what a human wants to see in a log line.
    """
    off = pd.Timestamp(int(ms), unit="ms", tz="UTC").tz_convert(tz).utcoffset()
    return int(off.total_seconds() // 3600)
