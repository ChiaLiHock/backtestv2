"""Screening engine — the shared tool every mining pass uses.

Three ideas make the numbers here honest, and none of them are optional:

1. **Pooled long+short at a SYMMETRIC bracket.** P(long wins) + P(short wins) = 1
   on any bar, so the pooled win rate of a *random* rule is 49.8% whatever the
   market did. That is the null. A long-only 56% is evidence of nothing.
2. **Non-overlapping sequential trades.** A candidate is skipped while a position
   is open. Bar-level counting reuses the same trend ten times and its
   confidence intervals lie by a factor of three.
3. **Multiple testing is counted.** Every screen reports how many hypotheses it
   tested and applies Benjamini-Hochberg. Cross-panel replication is applied on
   top, because it is stronger than any p-value correction.

CLI:
    python screen.py bins   PANEL FEATURE [--base] [--tp 2.5 --sl 2.5]
    python screen.py cands  PANEL CANDFILE.json [--base]
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

SP = r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\mine"

FEE_BPS_RT = 11.0
SLIP_TICKS = 2
TICK = 0.01
NULL_WR = 49.8

TF_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}
PATH_MIN = {"XAUUSDT": 1, "BTCUSDT": 1, "ETHUSDT": 1, "PAXGUSDT": 5, "MT5GOLD": 5}

# Symbols whose round-trip cost is a SPREAD CROSSING, not an exchange fee.
# MT5 `GOLD` at XM is spread-only on a Standard account: you buy the ask and
# sell the bid, so the round trip costs one spread, which the broker records on
# every bar (median 0.38 = 0.83 bps — AUTOMATION.md §1). Pricing this panel at
# the Bybit 11 bps would put break-even ~13 points too high and fail the §5.1
# gate for free. `spread_price` comes from the panel when build.py found it;
# the fallback is the measured all-hours median.
SPREAD_COST_SYMBOLS = {"MT5GOLD": 0.38}


# ---------------------------------------------------------------------------

class Panel:
    def __init__(self, symbol: str, anchor: str, years: tuple[int, ...] | None = None):
        self.symbol, self.anchor = symbol, anchor
        d = pd.read_pickle(f"{SP}\\panel_{symbol}_{anchor}.pkl")
        if years:
            d = d[d.year.isin(years)].reset_index(drop=True)
        self.d = d
        self.anchor_ms = TF_MIN[anchor] * 60_000
        self.path_ms = PATH_MIN[symbol] * 60_000
        self.label = f"{symbol.replace('USDT','')} {anchor}" + (
            f" {years[0]}-{years[-1]}" if years else "")

    def __len__(self):
        return len(self.d)

    # -- the reference rule from docs/ENTRY_RULES.md ----------------------
    def base(self, use_5m=True, look=3, ema="ema_14"):
        d = self.d
        tfs = ["5m", "15m", "30m", "1h", "4h"] if use_5m else ["15m", "30m", "1h", "4h"]
        L = pd.Series(True, index=d.index); S = L.copy()
        for tf in tfs:
            L &= (d[f"bias_{tf}"] == 1); S &= (d[f"bias_{tf}"] == -1)
        L &= (d["4h_ema_alignment"] == 1); S &= (d["4h_ema_alignment"] == -1)
        rl = lambda s, n: s.rolling(n, min_periods=1).max().astype(bool)
        L &= rl(d.low < d[ema], look) & (d.close > d[ema]) & (d.close > d.close.shift(1))
        S &= rl(d.high > d[ema], look) & (d.close < d[ema]) & (d.close < d.close.shift(1))
        return L.fillna(False), S.fillna(False)

    def none(self):
        t = pd.Series(True, index=self.d.index)
        return t, t

    # -- the user's trading windows (MYT = UTC+8) --------------------------
    # A  06:00-09:00 MYT = 22:00-01:00 UTC : NY close -> Asia open. QUIET.
    #    ATR% 0.143, turnover $370k/bar, relative volume 0.93.
    # B  19:00-22:00 MYT = 11:00-14:00 UTC : London PM -> NY open. PRIME.
    #    ATR% 0.192, turnover $808k/bar (2.2x A), relative volume 1.19.
    # The volatility gap is not cosmetic: at a 2.5/2.5 ATR bracket it puts
    # window A's break-even ~5 points above window B's on the same instrument.
    def window(self, which: str = "AB"):
        h = self.d.hour_myt
        a = h.isin([6, 7, 8])
        b = h.isin([19, 20, 21])
        return {"A": a, "B": b, "AB": a | b, "OUT": ~(a | b)}[which]


def cost_of(px, symbol: str = "XAUUSDT", spread: float | None = None):
    """Round-trip cost per unit, in price.

    Defaults reproduce the original exchange-fee model exactly, so every panel
    measured before MT5 existed returns bit-identical numbers.
    """
    if symbol in SPREAD_COST_SYMBOLS:
        if spread is not None and np.isfinite(spread) and spread > 0:
            return float(spread)
        return SPREAD_COST_SYMBOLS[symbol]
    return px * FEE_BPS_RT / 10_000 + SLIP_TICKS * TICK


@dataclass
class Result:
    name: str
    n: int
    wr: float
    ci: float
    z: float
    wrL: float
    wrS: float
    nL: int
    nS: int
    pf: float
    evR: float
    ddR: float
    per_day: float
    be: float          # break-even win rate at this bracket, this panel
    hrs: float

    def row(self):
        return dict(rule=self.name, n=self.n, wr=self.wr, ci=self.ci, z=self.z,
                    wrL=self.wrL, wrS=self.wrS, be=self.be, edge=round(self.wr - self.be, 1),
                    pf=self.pf, evR=self.evR, ddR=self.ddR, hrs=self.hrs,
                    per_day=self.per_day)


def simulate(p: Panel, ml, ms, tp=2.5, sl=2.5, name="") -> Result | None:
    d = p.d
    ot = d.open_time.to_numpy("int64")
    atr = d.atr.to_numpy(); ent = d.entry.to_numpy()
    sess = d.in_session.to_numpy(bool)
    ml = np.asarray(pd.Series(ml).fillna(False), dtype=bool) & sess
    ms = np.asarray(pd.Series(ms).fillna(False), dtype=bool) & sess
    spr = (d["spread_price"].to_numpy("float64") if "spread_price" in d.columns
           else np.full(len(d), np.nan))
    wl = d[f"w_{tp}_{sl}"].to_numpy(); bl = d[f"b_{tp}_{sl}"].to_numpy()
    ws = d[f"s_{tp}_{sl}"].to_numpy(); bs = d[f"sb_{tp}_{sl}"].to_numpy()
    free = -1
    rec = []
    ambiguous = int((ml & ms).sum())
    for i in range(len(d) - 1):
        if ot[i] < free:
            continue
        # A bar that says both long and short carries no direction. Skipping it
        # is the only honest choice: silently preferring long would turn every
        # non-directional feature into a long-only test against a falling market.
        if ml[i] and ms[i]:
            continue
        side = 1 if ml[i] else (-1 if ms[i] else 0)
        if side == 0:
            continue
        w = wl[i] if side == 1 else ws[i]
        b = bl[i] if side == 1 else bs[i]
        if w < 0 or not np.isfinite(b) or not np.isfinite(atr[i]) or not np.isfinite(ent[i]):
            continue
        c = cost_of(ent[i], p.symbol, spr[i])
        pnl = (tp * atr[i] - c) if w == 1 else -(sl * atr[i] + c)
        rec.append((side, int(w), pnl, pnl / (sl * atr[i]), b, atr[i], ent[i], c))
        free = ot[i] + p.anchor_ms + b * p.path_ms
    if len(rec) < 8:
        return None
    _ = ambiguous
    t = pd.DataFrame(rec, columns=["side", "win", "pnl", "R", "pb", "atr", "px", "c"])
    days = (d.open_time.iloc[-1] - d.open_time.iloc[0]) / 86_400_000
    n = len(t); wr = t.win.mean() * 100
    eq = t.R.cumsum(); dd = float((eq.cummax() - eq).max())
    # Break-even must be computed from the ATR of the trades ACTUALLY TAKEN, not
    # from the panel median. Selecting a subset of hours changes the volatility:
    # 06-09 MYT runs 0.143% ATR against 0.192% at 19-22, and the panel median
    # would understate the first window's break-even by ~4 points.
    # Cost is taken the same way, from the trades actually taken. cost_of() is
    # linear in price, so on the fee-based panels median(cost) == cost(median
    # price) and every pre-existing number is unchanged to the last decimal.
    med_atr = float(t.atr.median())
    be = (sl * med_atr + float(t.c.median())) / ((tp + sl) * med_atr) * 100
    se = np.sqrt(0.25 / n) * 100
    return Result(
        name=name, n=n, wr=round(wr, 1), ci=round(1.96 * np.sqrt(max(wr*(100-wr), 1)/n), 1),
        z=round((wr - NULL_WR) / se, 2),
        wrL=round(t.win[t.side == 1].mean()*100, 1) if (t.side == 1).any() else float("nan"),
        wrS=round(t.win[t.side == -1].mean()*100, 1) if (t.side == -1).any() else float("nan"),
        nL=int((t.side == 1).sum()), nS=int((t.side == -1).sum()),
        pf=round(t.pnl[t.pnl > 0].sum() / max(-t.pnl[t.pnl < 0].sum(), 1e-9), 2),
        evR=round(t.R.mean(), 3), ddR=round(dd, 1),
        per_day=round(n / days, 2), be=round(be, 1),
        hrs=round(float(t.pb.mean()) * PATH_MIN[p.symbol] / 60, 1))


def bh_fdr(pvals, q=0.10):
    """Benjamini-Hochberg. Returns a boolean mask of discoveries."""
    p = np.asarray(pvals, dtype="float64")
    order = np.argsort(p)
    m = p.size
    thresh = q * (np.arange(1, m + 1) / m)
    passed = p[order] <= thresh
    k = np.max(np.where(passed)[0]) + 1 if passed.any() else 0
    out = np.zeros(m, dtype=bool)
    out[order[:k]] = True
    return out


def two_sided_p(z):
    from math import erfc, sqrt
    return erfc(abs(z) / sqrt(2))


def screen(p: Panel, cands: list[tuple[str, str, str]], base=None,
           tp=2.5, sl=2.5, q=0.10) -> pd.DataFrame:
    """cands = [(name, long_expr, short_expr)] evaluated with `d` in scope."""
    d = p.d
    env = {"d": d, "np": np, "pd": pd}
    bl, bs = base if base else p.none()
    rows = []
    for name, le, se in cands:
        try:
            L = eval(le, env) if isinstance(le, str) else le
            S = eval(se, env) if isinstance(se, str) else se
        except Exception as exc:
            rows.append(dict(rule=name, n=0, note=f"ERR {exc}"))
            continue
        r = simulate(p, pd.Series(L, index=d.index) & bl,
                     pd.Series(S, index=d.index) & bs, tp, sl, name)
        if r:
            rows.append(r.row())
    out = pd.DataFrame(rows)
    if "z" in out:
        out["p"] = [two_sided_p(z) if pd.notna(z) else 1.0 for z in out.z]
        out["fdr_pass"] = bh_fdr(out.p.fillna(1.0).to_numpy(), q)
    out.attrs["k_tested"] = len(cands)
    return out


def bins(p: Panel, feature: str, base=None, tp=2.5, sl=2.5, nq=5,
         mirror: str | None = None) -> pd.DataFrame:
    """Quantile-bin a numeric feature and report the win rate per bin.

    A monotone response across bins is credible. One hot bin surrounded by cold
    ones is noise, however good its p-value looks.

    `mirror` names the short-side column when the feature is directional
    (e.g. feature='div_macd_hid_bull', mirror='div_macd_hid_bear').
    """
    d = p.d
    bl, bs = base if base else p.none()
    fl = d[feature]
    fs = d[mirror] if mirror else fl
    if fl.dropna().nunique() <= 3:
        edges = sorted(fl.dropna().unique())
        groups = [(f"{feature}=={v}", fl == v, fs == v) for v in edges]
    else:
        qs = np.linspace(0, 1, nq + 1)
        e = np.array(fl.quantile(qs).to_numpy(), dtype="float64", copy=True)
        e[0] = -np.inf; e[-1] = np.inf
        groups = []
        for i in range(nq):
            lo, hi = e[i], e[i + 1]
            groups.append((f"[{lo:.3g},{hi:.3g})",
                           (fl >= lo) & (fl < hi), (fs >= lo) & (fs < hi)))
    rows = []
    for nm, L, S in groups:
        r = simulate(p, L.fillna(False) & bl, S.fillna(False) & bs, tp, sl, nm)
        if r:
            rows.append(r.row())
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------

def _load(spec: str) -> Panel:
    """'XAUUSDT/15m' or 'PAXGUSDT/1h/2025,2026'"""
    parts = spec.split("/")
    yrs = tuple(int(y) for y in parts[2].split(",")) if len(parts) > 2 else None
    return Panel(parts[0], parts[1], yrs)


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 500)
    pd.set_option("display.max_columns", 40)
    mode = sys.argv[1]
    panel = _load(sys.argv[2])
    use_base = "--base" in sys.argv
    tp = float(sys.argv[sys.argv.index("--tp") + 1]) if "--tp" in sys.argv else 2.5
    sl = float(sys.argv[sys.argv.index("--sl") + 1]) if "--sl" in sys.argv else 2.5
    b = panel.base() if use_base else None
    print(f"# {panel.label}  n_bars={len(panel)}  TP{tp}/SL{sl}  "
          f"base={'ENTRY_RULES' if use_base else 'none'}  null WR={NULL_WR}%")
    if mode == "bins":
        mir = sys.argv[sys.argv.index("--mirror") + 1] if "--mirror" in sys.argv else None
        print(bins(panel, sys.argv[3], b, tp, sl, mirror=mir).to_string(index=False))
    elif mode == "cands":
        cands = [tuple(c) for c in json.load(open(sys.argv[3], encoding="utf-8"))]
        out = screen(panel, cands, b, tp, sl)
        print(f"# {out.attrs['k_tested']} hypotheses tested, BH-FDR q=0.10")
        print(out.sort_values("wr", ascending=False).to_string(index=False))
