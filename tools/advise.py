"""Position advisor — where the rule says the levels are, for positions you already hold.

    python -m backtest.tools.advise
    python -m backtest.tools.advise --tf 15m --json

Reads open positions from ``broker_trades`` and prints, for each one, the levels
`docs/ENTRY_RULES.md` §5 option B implies: a stop frozen at 2.5x the entry bar's
ATR, a partial at 2.5x, and a chandelier trail 2.5x below the running peak once
the partial has filled. It also checks whether the entry conditions that justify
*holding* are still true.

**This computes a rule, it does not give advice.** Every number is a mechanical
consequence of the config; nothing here is a judgement about what you should do.
The positions in `broker_trades` were not necessarily opened by the rule, so the
levels are "where the rule would have put them", not a reconstruction of intent.

Why this is deterministic and not a language model: the whole point of
`ENTRY_RULES.md` is that the exit is decided in advance. A model re-deciding the
stop every five minutes would put variance back into the one part of the system
that was made systematic on purpose.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, r"C:\inetpub\Claude")
from ITSupport.backtest.indicators.registry import compute_indicators  # noqa: E402

DB = r"C:\inetpub\Claude\ITSupport\backtest\data\market.db"
MYT = timezone(timedelta(hours=8))

# ENTRY_RULES.md §5 option B
SL_ATR = 2.5
TP1_ATR = 2.5
TRAIL_ATR = 2.5
PARTIAL = 0.5

TFS = ("5m", "15m", "30m", "1h", "4h")


def load_tf(con, symbol, tf):
    d = pd.read_sql_query(
        "select open_time,open,high,low,close,volume from candles "
        "where symbol=? and interval=? order by open_time",
        con, params=(symbol, tf))
    return d, compute_indicators(d, tf)


def infer_contract_size(con, symbol) -> float | None:
    """Recover lot size from closed trades rather than assuming it.

    profit ~= (close - open) * side * volume * contract_size, so the median of
    profit / ((close-open)*side*volume) is the contract size the broker used.
    Assuming 1.0 would misstate gold risk by 100x.
    """
    q = ("select side,volume,open_price,close_price,profit from broker_trades "
         "where symbol=? and close_time is not null and profit is not null "
         "and volume>0")
    rows = con.execute(q, (symbol,)).fetchall()
    vals = []
    for side, vol, op, cp, pr in rows:
        if op is None or cp is None or op == cp:
            continue
        s = 1 if side == "buy" else -1
        denom = (cp - op) * s * vol
        if abs(denom) > 1e-9:
            vals.append(pr / denom)
    if len(vals) < 5:
        return None
    return float(pd.Series(vals).median())


def thesis(ind: dict, side: str) -> list[tuple[str, bool]]:
    """The legs of ENTRY_RULES.md §4 that justify HOLDING, in slowest-first order."""
    want = 1 if side == "buy" else -1
    legs = [(f"tf_{tf}_ut_{'bullish' if want == 1 else 'bearish'}",
             int(ind[tf]["ut_bias"]) == want) for tf in ("4h", "1h", "30m", "15m", "5m")]
    legs.append((f"tf_4h_ema_stack_{'bullish' if want == 1 else 'bearish'}",
                 int(ind["4h"]["ema_alignment"]) == want))
    return legs


def advise(symbol_filter=None, anchor="15m", as_json=False):
    con = sqlite3.connect(DB)
    open_rows = con.execute(
        "select account,position_id,symbol,broker_symbol,side,volume,open_time,"
        "open_price,sl,tp from broker_trades where close_time is null "
        "order by open_time").fetchall()
    if symbol_filter:
        open_rows = [r for r in open_rows if r[2] == symbol_filter]
    if not open_rows:
        print("no open positions in broker_trades")
        return

    cache: dict[str, tuple] = {}
    out = []
    for (acct, pid, symbol, bsym, side, vol, ot, op, sl, tp) in open_rows:
        if symbol not in cache:
            frames = {tf: load_tf(con, symbol, tf) for tf in TFS}
            m1 = pd.read_sql_query(
                "select open_time,high,low,close from candles where symbol=? "
                "and interval='1m' order by open_time", con, params=(symbol,))
            cache[symbol] = (frames, m1, infer_contract_size(con, symbol))
        frames, m1, csize = cache[symbol]
        raw_a, ind_a = frames[anchor]

        # ATR frozen at the bar the position opened in
        k = int(raw_a.open_time.searchsorted(ot, side="right")) - 1
        k = max(k, 0)
        atr_entry = float(ind_a["atr_14"].iloc[k])
        last_ind = {tf: frames[tf][1].iloc[-1] for tf in TFS}
        px = float(m1.close.iloc[-1]) if len(m1) else float(raw_a.close.iloc[-1])

        s = 1 if side == "buy" else -1
        risk_px = SL_ATR * atr_entry
        rule_sl = op - s * risk_px
        rule_tp1 = op + s * TP1_ATR * atr_entry

        path = m1[m1.open_time >= ot]
        peak = (float(path.high.max()) if s == 1 else float(path.low.min())) if len(path) else op
        excursion_atr = (peak - op) * s / atr_entry
        tp1_hit = excursion_atr >= TP1_ATR
        trail = peak - s * TRAIL_ATR * atr_entry
        stop_now = max(op, trail) if s == 1 else min(op, trail)   # breakeven after TP1

        r_now = (px - op) * s / risk_px
        legs = thesis(last_ind, side)
        n_ok = sum(1 for _, v in legs if v)

        out.append(dict(
            position=pid, symbol=symbol, broker_symbol=bsym, side=side, volume=vol,
            opened_myt=datetime.fromtimestamp(ot / 1000, MYT).strftime("%Y-%m-%d %H:%M"),
            entry=round(op, 2), price_now=round(px, 2),
            atr_at_entry=round(atr_entry, 3), contract_size=csize,
            broker_sl=sl, broker_tp=tp,
            rule_sl=round(rule_sl, 2), rule_tp1=round(rule_tp1, 2),
            risk_per_unit=round(risk_px, 2),
            risk_money=round(risk_px * vol * csize, 2) if csize else None,
            r_now=round(r_now, 2),
            best_excursion_atr=round(excursion_atr, 2),
            tp1_filled=tp1_hit,
            trail_level=round(trail, 2) if tp1_hit else None,
            stop_in_force=round(stop_now, 2) if tp1_hit else round(rule_sl, 2),
            thesis_legs=[{"leg": n, "ok": v} for n, v in legs],
            thesis_intact=n_ok == len(legs),
            thesis_score=f"{n_ok}/{len(legs)}",
        ))

    if as_json:
        print(json.dumps(out, indent=1, default=str))
        return

    for p in out:
        print(f"\n{'='*78}")
        print(f"{p['symbol']} ({p['broker_symbol']})  {p['side'].upper()}  {p['volume']} lots"
              f"   position {p['position']}")
        print(f"  opened {p['opened_myt']} MYT @ {p['entry']}    now {p['price_now']}"
              f"    P/L {p['r_now']:+.2f} R")
        cs = p["contract_size"]
        print(f"  ATR-14 at entry ({anchor}) = {p['atr_at_entry']}"
              f"   contract size inferred = {cs if cs else 'UNKNOWN'}")
        print(f"\n  what the rule says (ENTRY_RULES.md §5B):")
        print(f"    stop      {p['rule_sl']:>10}   ({SL_ATR} x ATR from entry, frozen)")
        print(f"    partial   {p['rule_tp1']:>10}   ({TP1_ATR} x ATR, close {int(PARTIAL*100)}%,"
              f" then stop -> breakeven)")
        if p["tp1_filled"]:
            print(f"    trail     {p['trail_level']:>10}   (armed: peak {TRAIL_ATR} x ATR back)")
        else:
            print(f"    trail       not armed   (partial not reached;"
                  f" best excursion {p['best_excursion_atr']} ATR)")
        print(f"    STOP IN FORCE {p['stop_in_force']:>8}")
        rm = p["risk_money"]
        print(f"    risk      {p['risk_per_unit']} per unit"
              + (f"  =  {rm} account currency at this size" if rm is not None else ""))
        bsl, btp = p["broker_sl"], p["broker_tp"]
        if not bsl:
            print(f"    !! THIS POSITION HAS NO STOP AT THE BROKER."
                  f" The rule's stop would be {p['rule_sl']}.")
        elif abs(bsl - p["rule_sl"]) > 0.25 * p["atr_at_entry"]:
            print(f"    !! broker stop {bsl} vs rule stop {p['rule_sl']}")
        if btp:
            d_atr = abs(btp - p["entry"]) / p["atr_at_entry"]
            print(f"    broker TP {btp}  = {d_atr:.2f} x ATR"
                  + ("  (rule takes a partial at 2.5 and trails the rest)"
                     if d_atr < TP1_ATR else ""))
        print(f"\n  is the reason to HOLD still true?  {p['thesis_score']}"
              f"  {'INTACT' if p['thesis_intact'] else 'BROKEN'}")
        for leg in p["thesis_legs"]:
            print(f"    {'OK ' if leg['ok'] else 'NO '} {leg['leg']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    advise(a.symbol, a.tf, a.json)
