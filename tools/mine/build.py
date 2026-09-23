"""Build one panel: every feature, plus first-touch outcome labels.

    python build.py SYMBOL ANCHOR PATH_TF HORIZON_PATH_BARS

Outcome labels are resolved by walking the forward path at PATH_TF resolution
and asking which of the target/stop is touched first. Ties resolve as LOSSES.
"""
from __future__ import annotations

import sys, sqlite3, time
import numpy as np
import pandas as pd

SP = r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\mine"
sys.path.insert(0, r"C:\inetpub\Claude")
sys.path.insert(0, SP)

from ITSupport.backtest.indicators.registry import compute_indicators
from ITSupport.backtest.engine import features as EF
import feats as FT

DB = r"C:\inetpub\Claude\ITSupport\backtest\data\market.db"
TFS = ["5m", "15m", "30m", "1h", "4h"]
TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}
TP_ATR = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0]
SL_ATR = [1.5, 2.0, 2.5, 3.0]
MFE_H = [60, 240, 720, 2880]          # minutes


def load(sym, tf, con):
    return pd.read_sql_query(
        "select open_time,open,high,low,close,volume,turnover from candles "
        "where symbol=? and interval=? order by open_time", con, params=(sym, tf))


def load_spread(sym, tf, con):
    """Broker per-bar spread, in PRICE, keyed on the same open_time as `candles`.

    Only MT5 rows have this. On a broker feed the spread IS the round-trip cost,
    so break-even has to be computed from it rather than from an exchange fee
    constant (AUTOMATION.md §1: median 0.38 on GOLD = 0.83 bps, against Bybit's
    11 bps). Returns an empty frame for any symbol that has no such record, and
    nothing downstream changes for those.
    """
    try:
        return pd.read_sql_query(
            "select open_time, spread_price from mt5_bar_spread "
            "where symbol=? and interval=? order by open_time",
            con, params=(sym, tf))
    except Exception:
        return pd.DataFrame(columns=["open_time", "spread_price"])


def per_tf_features(raw: pd.DataFrame, ind: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Features computed on a timeframe's OWN grid, before any alignment."""
    f = pd.DataFrame(index=ind.index)
    m = TF_MIN[tf]
    f["ut_flip_age_min"] = FT.flip_age_bars(ind["ut_bias"].to_numpy()) * m
    f["stack_flip_age_min"] = FT.flip_age_bars(ind["ema_alignment"].to_numpy()) * m

    o = raw["open"].to_numpy(); h = raw["high"].to_numpy()
    l = raw["low"].to_numpy();  c = raw["close"].to_numpy()
    v = raw["volume"].to_numpy(dtype="float64")

    f["vol_pctile_100"] = FT.pct_rank(v, 100)
    f["turnover"] = raw["turnover"].to_numpy(dtype="float64")
    f["body_ratio"] = FT.body_ratio(o, h, l, c)
    f["close_pos"] = FT.close_position(o, h, l, c)
    f["adx_slope_5"] = FT.slope(ind["adx_14"].to_numpy(), 5)
    f["rsi_slope_5"] = FT.slope(ind["rsi_14"].to_numpy(), 5)
    f["macd_hist_slope_3"] = FT.slope(ind["macd_hist"].to_numpy(), 3)
    f["sep_slope_10"] = FT.slope(ind["ema_separation_atr"].to_numpy(), 10)

    # divergences: price vs MACD line, and price vs RSI
    L = R = 3 if tf in ("5m", "15m") else 2
    for osc_name, osc in (("macd", ind["macd"].to_numpy()),
                          ("rsi", ind["rsi_14"].to_numpy())):
        for kind in ("reg_bear", "reg_bull", "hid_bear", "hid_bull"):
            src = h if kind in ("reg_bear", "hid_bear") else l
            f[f"div_{osc_name}_{kind}"] = FT.divergence(src, osc, L, R, kind, max_age=20)

    # pullback geometry relative to the EMAs, in ATR
    atr = ind["atr_14"].to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        f["low_vs_ema14_atr"] = (l - ind["ema_14"].to_numpy()) / atr
        f["high_vs_ema14_atr"] = (h - ind["ema_14"].to_numpy()) / atr
        f["close_vs_ut_atr"] = (c - ind["ut_level"].to_numpy()) / atr
        f["close_vs_vwap_atr"] = (c - ind["vwap"].to_numpy()) / atr
        f["dip_depth_atr_5"] = (FT.rolling_min(l, 5) - ind["ema_14"].to_numpy()) / atr
        f["pop_height_atr_5"] = (FT.rolling_max(h, 5) - ind["ema_14"].to_numpy()) / atr
        f["rsi_min_5"] = FT.rolling_min(ind["rsi_14"].to_numpy(), 5)
        f["rsi_max_5"] = FT.rolling_max(ind["rsi_14"].to_numpy(), 5)
    return f


def main():
    sym, anchor, path_tf, hp = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    path_ms = TF_MIN[path_tf] * 60_000
    t0 = time.time()
    con = sqlite3.connect(DB)

    raw = {tf: load(sym, tf, con) for tf in set(TFS + [path_tf, anchor])}
    ind = {tf: compute_indicators(raw[tf], tf) for tf in TFS}
    own = {tf: per_tf_features(raw[tf], ind[tf], tf) for tf in TFS}
    print(f"  indicators+features {time.time()-t0:.0f}s", file=sys.stderr)

    a = raw[anchor]
    a_ot = a["open_time"].to_numpy("int64")
    a_op = a["open"].to_numpy("float64")

    frames = {tf: (raw[tf]["open_time"].to_numpy("int64"), ind[tf]) for tf in TFS}
    idxmap = EF.build_htf_alignment(a_ot, anchor, frames)

    cols: dict[str, np.ndarray] = {"open_time": a_ot}

    def take(tf, frame, prefix):
        al = EF.aligned_frame(frame, idxmap[tf])
        for c in frame.columns:
            if c == "open_time":
                continue
            cols[f"{prefix}{c}"] = al[c].to_numpy()

    for tf in TFS:
        pre = "" if tf == anchor else f"{tf}_"
        take(tf, ind[tf], pre)
        take(tf, own[tf], pre)

    # raw anchor OHLC
    for c in ("open", "high", "low", "close", "volume", "turnover"):
        cols[c] = a[c].to_numpy("float64")

    d = pd.DataFrame(cols)

    # ---- real per-bar execution cost, where the feed records one ---------
    sp = load_spread(sym, anchor, con)
    if len(sp):
        d = d.merge(sp, on="open_time", how="left")
        print(f"  spread_price attached: {int(d.spread_price.notna().sum())}/"
              f"{len(d)} bars, median {d.spread_price.median():.3f}", file=sys.stderr)

    # ---- cross-timeframe cascade features --------------------------------
    age = {tf: d[("ut_flip_age_min" if tf == anchor else f"{tf}_ut_flip_age_min")]
           for tf in TFS}
    bias = {tf: d[("ut_bias" if tf == anchor else f"{tf}_ut_bias")] for tf in TFS}
    for tf in TFS:
        d[f"age_{tf}"] = age[tf]
        d[f"bias_{tf}"] = bias[tf]
    d["lag_5m_15m"] = age["5m"] - age["15m"]
    d["lag_15m_1h"] = age["15m"] - age["1h"]
    d["lag_5m_4h"] = age["5m"] - age["4h"]
    d["age_min_all"] = pd.concat([age[t] for t in TFS], axis=1).min(axis=1)
    d["age_max_all"] = pd.concat([age[t] for t in TFS], axis=1).max(axis=1)
    agree_up = sum((bias[t] == 1).astype(int) for t in TFS)
    agree_dn = sum((bias[t] == -1).astype(int) for t in TFS)
    d["n_bull"] = agree_up
    d["n_bear"] = agree_dn
    # ordered cascade: faster timeframes flipped LONGER ago than slower ones
    d["cascade_fast_first"] = (
        (age["5m"] >= age["15m"]) & (age["15m"] >= age["30m"]) &
        (age["30m"] >= age["1h"]) & (age["1h"] >= age["4h"]))

    # ---- time ------------------------------------------------------------
    ts = pd.to_datetime(d.open_time, unit="ms", utc=True)
    d["dow"] = ts.dt.dayofweek
    d["hour_utc"] = ts.dt.hour
    d["hour_myt"] = (ts + pd.Timedelta(hours=8)).dt.hour
    d["year"] = ts.dt.year
    d["in_session"] = ~((d.dow == 5) | ((d.dow == 4) & (d.hour_utc >= 22)) |
                        ((d.dow == 6) & (d.hour_utc < 21)))

    # ---- outcome labels --------------------------------------------------
    p = raw[path_tf]
    P_OT = p["open_time"].to_numpy("int64")
    P_HI = p["high"].to_numpy(); P_LO = p["low"].to_numpy()
    n = len(a)
    ent = np.full(n, np.nan); ent[:-1] = a_op[1:]
    start = np.searchsorted(P_OT, np.r_[a_ot[1:], a_ot[-1]], side="left")
    atr = d["atr_14"].to_numpy()

    LW = {k: np.full(n, -1, "int8") for k in [(t, s) for t in TP_ATR for s in SL_ATR]}
    LB = {k: np.full(n, np.nan, "float32") for k in LW}
    SW = {k: np.full(n, -1, "int8") for k in LW}
    SB = {k: np.full(n, np.nan, "float32") for k in LW}
    mfe = {h: np.full(n, np.nan, "float32") for h in MFE_H}
    mae = {h: np.full(n, np.nan, "float32") for h in MFE_H}
    per_bar = TF_MIN[path_tf]

    for i in range(n - 1):
        e = ent[i]; A = atr[i]
        if not np.isfinite(e) or not np.isfinite(A) or A <= 0:
            continue
        s = start[i]
        if s >= P_OT.size:
            continue
        # The path series must actually COVER the entry. On the Bybit panels it
        # always does. On MT5GOLD it does not: the broker serves ~100k bars per
        # timeframe, so 15m reaches 2022 while 5m only reaches 2025-03-25. For
        # an entry before the path series starts, searchsorted returns 0 and the
        # loop below would walk a window of prices from YEARS LATER against a
        # 2022 entry price and label it confidently. Leaving those rows
        # unlabelled (-1) is what makes the panel honest; simulate() skips them.
        if P_OT[s] - a_ot[i + 1] > path_ms:
            continue
        j = min(s + hp, P_OT.size)
        hi = np.maximum.accumulate(P_HI[s:j])
        lo = np.minimum.accumulate(P_LO[s:j])
        L = hi.size
        nlo = -lo
        for hz in MFE_H:
            k = min(hz // per_bar, L) - 1
            if k >= 0:
                mfe[hz][i] = (hi[k] - e) / A
                mae[hz][i] = (e - lo[k]) / A
        up = {t: np.searchsorted(hi, e + t * A, side="left") for t in set(TP_ATR + SL_ATR)}
        dn = {t: np.searchsorted(nlo, -(e - t * A), side="left") for t in set(TP_ATR + SL_ATR)}
        for tp in TP_ATR:
            itp, stp = up[tp], dn[tp]
            for sl in SL_ATR:
                isl, ssl = dn[sl], up[sl]
                if itp < L or isl < L:
                    LW[(tp, sl)][i] = 1 if itp < isl else 0
                    LB[(tp, sl)][i] = min(itp, isl)
                if stp < L or ssl < L:
                    SW[(tp, sl)][i] = 1 if stp < ssl else 0
                    SB[(tp, sl)][i] = min(stp, ssl)
        if i % 20000 == 0 and i:
            print(f"    labels {i}/{n} {time.time()-t0:.0f}s", file=sys.stderr)

    d["entry"] = ent
    d["atr"] = atr
    for hz in MFE_H:
        d[f"mfe_{hz}"] = mfe[hz]; d[f"mae_{hz}"] = mae[hz]
    for tp in TP_ATR:
        for sl in SL_ATR:
            d[f"w_{tp}_{sl}"] = LW[(tp, sl)]; d[f"b_{tp}_{sl}"] = LB[(tp, sl)]
            d[f"s_{tp}_{sl}"] = SW[(tp, sl)]; d[f"sb_{tp}_{sl}"] = SB[(tp, sl)]
    d.attrs["symbol"] = sym; d.attrs["anchor"] = anchor
    d.attrs["path_ms"] = path_ms; d.attrs["anchor_ms"] = TF_MIN[anchor] * 60_000

    out = f"{SP}\\panel_{sym}_{anchor}.pkl"
    d.to_pickle(out)
    print(f"{sym} {anchor}: {len(d)} rows, {len(d.columns)} cols, "
          f"path={path_tf}, {time.time()-t0:.0f}s -> {out}")


def check_causal(sym: str, tf: str, cuts=(0.35, 0.55, 0.75, 0.90)) -> int:
    """Truncate the input, recompute, require every earlier value to be identical.

    This is the check `feats.py` advertises. A feature that peeks at future bars
    will change its own past when the future is removed, and nothing else in this
    pipeline would notice.
    """
    con = sqlite3.connect(DB)
    raw = load(sym, tf, con)
    full = per_tf_features(raw, compute_indicators(raw, tf), tf)
    bad = 0
    for c in cuts:
        k = int(len(raw) * c)
        part = raw.iloc[:k].reset_index(drop=True)
        got = per_tf_features(part, compute_indicators(part, tf), tf)
        for col in full.columns:
            a = full[col].to_numpy()[:k]
            b = got[col].to_numpy()
            same = ((a == b) | (pd.isna(a) & pd.isna(b))).all()
            if not same:
                n = int((~((a == b) | (pd.isna(a) & pd.isna(b)))).sum())
                print(f"  LOOK-AHEAD  {sym} {tf} cut={c} col={col}: {n} rows differ")
                bad += 1
    print(f"{sym} {tf}: {len(full.columns)} columns x {len(cuts)} cut points -> "
          f"{'CLEAN' if not bad else f'{bad} FAILURES'}")
    return bad


if __name__ == "__main__":
    if "--check-causal" in sys.argv:
        i = sys.argv.index("--check-causal")
        raise SystemExit(1 if check_causal(sys.argv[i + 1], sys.argv[i + 2]) else 0)
    main()
