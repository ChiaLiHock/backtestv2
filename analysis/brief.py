"""The "Copy analysis" blob — the mandate, the snapshot, and the reading notes.

The **Copy analysis** button hands an outside model three things in one paste:

1. **The mandate** — the owner's own instruction set: the macro-desk persona, the
   engine's scoring rules that must NOT be replaced with outside convention, the
   session timing rules, and the required output shape. Held in `MANDATE` below,
   in Traditional Chinese, because that is the language the answer must come back
   in and an instruction set written in one language and answered in another
   drifts.
2. **The snapshot** — every number a rule in the mandate reads. Gathered by
   `analysis/context.py`, which exists precisely so no rule has to answer
   "數據不足" about a value the database already holds.
3. **The reading notes** — what each number is and, more importantly, is not.

## Why the mandate separates two kinds of "don't know"

The engine's own scoring (ADX weighting, the volume bands, the funding baseline)
is **fixed and must be obeyed literally** — reading in an outside convention like
"below 1.0x volume is weak" changes the verdict on every bar between 0.8 and 1.2,
and the engine has no 1.0 line at all.

Market background (what an event actually is, what is driving gold tonight) is a
**different kind of gap**: externally checkable, and the mandate explicitly
authorises going and checking rather than stopping at "insufficient data".

Conflating those two was the failure this version fixes. A model told only "do
not go outside the data" stalls on questions it could have answered.

## Why the "what is absent" section is not optional

A model handed a factor labelled "Order-Book 30%" will reason about an order
book. There is none. It will reason about a `NEXT IMPORTANT EVENT` field. There
is no calendar in this project at all. Left unsaid, a model does not decline — it
writes confident prose about liquidity and events it cannot see, and that prose
is indistinguishable from the parts that are real. So every gap is named, and
named as a gap rather than as an empty value.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

MYT = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# The mandate. Owner-authored; edit the three blocks independently.
# ---------------------------------------------------------------------------

MANDATE = """\
================================================================================
【AI 角色與任務設定 / AI ROLE & MANDATE】
================================================================================
你現在是管理百億美金的全球頂級宏觀對沖基金（Macro Hedge Fund）首席清算操盤手。
請完全拋棄散戶的「指標看漲跌」思維。你的任務是解讀下方導出的數據快照，
以極度冷血、量化的機構視角進行交叉推演。

你有查證權限，且在以下情況下應主動使用，不需要等待額外指示：

1. NEXT IMPORTANT EVENT 的 impact 分級看起來與事件性質不符時
   （例如：政治性記者會、緊急制裁公告、非例行性談話，被標成 MEDIUM 或更低）
   → 主動搜尋該事件的實際性質與可能的市場影響範圍，並在輸出中修正你自己對它的定性，
   同時明確標註「引擎標籤與實際評估不符」。
   ⚠️ 本專案沒有任何經濟數據行事曆（見下方 ABSENT 區塊）。這一條的意思是：
   事件相關的空白必須主動查證，不可以把「快照裡沒有」讀成「今晚沒有事件」。

2. 當前價格行為的宏觀驅動力不明確時
   （例如：金價與殖利率同向、與美元指數背離、單日波動異常放大）
   → 主動查證當下的宏觀背景（美元指數、公債殖利率、原油、避險情緒），
   判斷這是「debasement trade」「降息交易」「地緣避險」還是「純技術性移動」，
   因為同樣的技術指標在不同宏觀驅動下，延續與反轉的機率結構不同。

3. 快照數據內部出現矛盾，且無法從快照本身判斷原因時
   （例如：TECHNICAL STATE 與 CONTEXT 的價格方向矛盾、KEY LEVELS 寬度為零、
   Funding 標籤與相對基準值矛盾）
   → 不要只在文字裡標註「這是 bug」。明確指出矛盾的具體數字，並說明：
   這個矛盾在多大程度上會影響你這次的結論可信度。

查證原則：
- 查證是為了驗證或推翻你對「這組指標組合意味著什麼」的判斷，不是為了另起爐灶做總體經濟報告。
- 查到的資訊要收斂回四個核心模組，不要另開一個「宏觀背景」的獨立長篇區塊，
  除非事件本身就是今晚的主要驅動力。
- 查證後如果結論沒有改變，仍要明說「已查證，結論不變」，讓使用者知道這一步確實做過。

================================================================================
【核心解讀邏輯 / CORE INTERPRETATION LOGIC】
================================================================================
以下是引擎自己的計分規則，必須精確遵守，不能套用外部技術分析的慣例。

1. ADX（趨勢強度）：ADX < 20 時引擎判定為震盪市，不承認任何趨勢。
   ADX 沒有「突破某個數字就算趨勢」的門檻。超過 20 之後是線性加權，到 40 才是滿權重
   ——ADX 26 不是「趨勢成立」，而是約 65% 的方向票重。
   同時檢視最近 4 根的 ADX 走勢（快照的 adx.last_bars / adx.slope 已提供）。
   ADX=20 在上升途中和在下降途中是相反訊號，只看單點數值會誤判。
   若快照顯示 slope 為 null，在輸出中註明「ADX 斜率未知，僅能讀單點數值」。

2. Tick Volume（相對於 20 根均量的倍數）：引擎分檔為
   ≥ 1.5x 強勢參與 · ≥ 1.2x 有參與 · 0.8–1.2x 中性偏弱 · 0.5–0.8x 明顯偏弱 · < 0.5x 極度縮量。
   請勿使用「低於 1.0x 即為虛胖」這類外部慣例——引擎沒有 1.0 這條線。
   當 trend_strength > 85 但 Tick volume 落在中性偏弱以下時，
   快照的 volume.participation_divergence 會標記為 true：
   在盤口動能判定模組中明確標記「參與度背離」，並說明這代表分數可能主要由結構性指標
   （EMA、UT、多週期一致性）撐起，而非真實成交參與撐起。

3. BB %B（布林帶位置）與多週期共振（alignment_summary）：結合上述兩者，
   判斷當前是爆發性單邊，還是縮量爬坡的震盪市。

4. CONTEXT 區塊：先讀它再下任何結論。同一組 ADX/%B/Volume
   在「暴漲後的高位盤整」與「什麼都沒發生的週末縮量」下數值可以完全相同，
   只有 CONTEXT 能分辨。

5. Funding Rate 一律先做基準比較，才能判讀「擁擠」與否。
   不同商品的中性基準不同（金約 +0.0100%）。快照已經算好
   funding.deviation_pct = 實際值 − 基準值；只有這個差值明顯偏離
   （門檻 ±0.0200%）才視為真正的擁擠訊號；否則即使別處標籤寫著「擁擠」，
   也要在輸出中註明「相對基準為中性」。

6. OI（未平倉量）四象限：快照的 open_interest.quadrant 已用下列模型算好——
       價漲 + OI 增 = 新多進場（真延續）
       價漲 + OI 減 = 空頭回補（彈藥有限，易衰竭）
       價跌 + OI 增 = 新空進場（真確認）
       價跌 + OI 減 = 多頭清算（常見於低點附近）
   若 open_interest.available 為 false，明說「無法計算 ΔOI，無法判斷象限」。

================================================================================
【跨盤口洗盤時間特性 / CROSS-SESSION TIMING RULES】
================================================================================
先讀 CONTEXT 的 session 欄位。若 session.timing_rules_apply 為 false
（週末或休市），則以下三條全部不適用，不要套用、也不要假裝它們仍然成立。
時間一律為馬來西亞時間（MYT = UTC+8）：

- 歐盤開盤（3:00 PM – 5:00 PM）：此階段若缺乏成交量，
  極易出現「假突破/假拉升」以掃蕩前期高位流動性。
- 數據發布窗口（8:30 PM）：美盤重磅數據（如 CPI、非農）公佈，波動率極限放大。
- 美股開盤（9:30 PM）：華爾街現貨資金全面進場，
  此時才會揭曉美盤交易員的真正資金方向。

若有任何重大事件落在接下來 8 小時內（或使用者說明的持倉/決策時間窗口內），
明確標註「事件在決策窗口內，歷史型態的機率統計基礎可能不適用」，
不論該事件的 impact 分級是什麼。本專案不提供行事曆，此判斷需要你主動查證。

================================================================================
【輸出格式要求 / OUTPUT FORMAT】
================================================================================
1. 請完全省略任何客套話與基礎術語解釋。
2. 必須使用繁體中文（華語）輸出。
3. 必須高度 scannable（易讀、多用粗體和簡短的短句/片段 fragment）。
4. 核心輸出為以下四個模組，這是每次都要有的骨架：
   🔥【盤口動能判定】· 🚨【流動性獵殺點】· 🛡️【失效紅線】· ⏳【機械化應對步驟】
5. 以下三個模組為條件觸發，只在對應情況成立時才輸出，不需要每次都有：
   📡【查證與外部驗證】——僅在你依據上方第 1–3 條主動查證外部資訊時輸出。
     說明你查了什麼、結果如何、對原本判讀有沒有造成修正。
   ⚠️【數據品質警示】——僅在快照內部出現矛盾、退化區間、stale 數據、
     或標籤與底層數字不符時輸出。明確指出具體欄位與數字，不要只說「有點怪」。
   📊【OI / 資金動態】——僅在 open_interest.available 為 true 時輸出。
     包含象限判定與其對主結論的影響。
6. 若某個核心模組的數據不足以支撐具體結論，直接輸出「數據不足」並說明缺什麼，
   禁止為了填滿格式而生成價位或方向。說「這裡看不出來」比猜一個數字有價值得多。
   快照可能來自週末、縮量、或剛切換市場後歷史不足的情況，這些時候沒有答案才是正確答案。
   假自信是這個工具最糟的失效模式——但「數據不足」不等於「不去查證外部可查的部分」：
   技術指標的空白只能承認，但事件性質、宏觀背景這類外部可查的空白，
   應該先查證再判斷是否真的不足。

--------------------------------------------------------------------------------
🔥【盤口動能判定：當前漲跌是有力還是沒力？】
核對 ADX（含斜率）、Tick Volume（含背離判定）、BB %B、多週期與 CONTEXT，
一錘定音點破當前市場能量，並明確指出此時應該 Aim「單邊突破」還是「區間震盪」。
若三者互相矛盾，說矛盾在哪，不要強行合成一個結論。
若判定過程中發現需要外部驗證的疑點（例如：技術面強但缺乏宏觀邏輯支撐），
在此處註明並視需要輸出【查證與外部驗證】模組。

🚨【流動性獵殺點：人性面看是強阻力/支撐，但其實可能是散戶墳墓的引爆點】
先判斷數據是否足以支撐這個判斷。若 KEY LEVELS 有明確區間、且價格確實靠近它，
才指出散戶最可能在哪裡死扛，以及機構可能把價格推向哪個位置去引爆止損。
若區間不明確或價格位在區間中段，請直接說「當前無明確獵殺點」
——不要為了填格式生成一個精準價位。
若 KEY LEVELS 出現零寬度或異常窄的區間，在此處標記並視需要輸出【數據品質警示】模組。

🛡️【今日大級別多空結構的生死防守底線（失效紅線）】
指出當前結構的終極失效價位。一旦放量實體跌破/突破哪裡，當前結構徹底崩塌，
必須切換為單邊單向破局思維？此欄若無法從 KEY LEVELS 與結構推出明確價位，
同樣輸出「數據不足」。

⏳【接下來 1–2 小時的機構假動作預警與機械化應對步驟】
結合 session 欄位判斷時間窗口是否適用，給出接下來 1–2 小時最冷血、精準的
1、2、3 條機械化操盤防線指令。若當前處於週末或無時段可依據，
請說明並改為給出「下次開盤前需要確認的 2–3 件事」。
若你查證後判定有重大事件落在此時間窗口內，機械化指令必須包含「事件前的部位處理原則」
（例如：是否該在事件前平倉、縮小倉位、或明確告知這是在賭事件方向而非統計交易）。
"""


HOW_TO_READ = """\
================================================================================
HOW TO READ THESE NUMBERS
================================================================================
- **Danger% and Confidence% are computed independently for each direction.**
  Short is NOT 100 minus long. Both can be bad at once — that is what a chop
  looks like, and it is how "no clear setup" happens here.
- **Confidence measures how much the evidence AGREES, 0-100.** It is not a
  probability, not a win rate, not a forecast.
- **Danger% comes from hand-set weights** (40% trend / 30% zones / 30% momentum)
  ported from an Android app. They have NEVER been measured on gold. Treat them
  as a description of the setup, not as odds.
- **The UT Dynamic Level is an ATR trailing stop, not support or resistance.**
  Nobody defended that price. The KEY LEVELS zones are the defended ones.
- **Every reading is from CLOSED bars only.** The forming bar is drawn but never
  produces a signal, because a level read off an unfinished bar can un-fire
  before that bar closes.
- **Correlated indicators share one category.** EMA alignment, the UT level and
  ADX direction are all trend-following and count as ONE trend reading, not
  three. `trend_strength` already blends them; do not re-count them separately.
- **External verification is also not a probability.** It tells you what macro
  backdrop the current technical reading was produced in. Direction and odds
  still have to come back to the snapshot's own evidence and, where present, the
  barrier statistics below.
- This is an analysis tool. It does not place trades and does not give financial
  advice.
"""


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _t(ms: int | None, tz=MYT, label="MYT") -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(int(ms) / 1000, tz).strftime("%Y-%m-%d %H:%M") + f" {label}"


def _n(v: Any, dp: int = 2, dash: str = "-") -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dash
    if f != f:
        return dash
    return f"{f:,.{dp}f}"


def _bar(pct: Any, width: int = 20) -> str:
    """A text meter, so the shape survives a paste into a plain-text box."""
    try:
        v = max(0, min(100, int(pct)))
    except (TypeError, ValueError):
        return "?" * width
    filled = round(v * width / 100)
    return "#" * filled + "." * (width - filled)


def _dur(ms: int | None) -> str:
    if ms is None:
        return "-"
    m = int(ms) // 60000
    if m < 0:
        return "0m"
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h < 48 else f"{h // 24}d{h % 24}h"


def _yn(v) -> str:
    return "yes" if v else "no" if v is not None else "-"


# ---------------------------------------------------------------------------
# The blob
# ---------------------------------------------------------------------------


def build(
    symbol: str,
    risk: dict | None,
    rule: dict | None,
    live: dict | None = None,
    positions: list[dict] | None = None,
    events: list[dict] | None = None,
    price: float | None = None,
    generated_ms: int | None = None,
    ctx: dict | None = None,
) -> str:
    """Mandate + snapshot + reading notes. Every argument may be absent."""
    L: list[str] = []
    a = L.append
    now = generated_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    ctx = ctx or {}
    meta = (risk or {}).get("meta") or {}
    # Resolved once, up here: the KEY LEVELS block prints price between the
    # resistance and support lists, and it is reachable even when the technical
    # section above was skipped for want of a price to print.
    px = price if price is not None else meta.get("spot")

    a(MANDATE)
    a("")
    a("=" * 80)
    a(f"DATA SNAPSHOT — {symbol}")
    a(f"generated {_t(now)}  ·  {_t(now, timezone.utc, 'UTC')}")
    a("=" * 80)
    a("")

    # -- absent inputs, before anything can be misread -----------------------
    a("-" * 80)
    a("ABSENT — read this before interpreting anything below")
    a("-" * 80)
    a("* NO ECONOMIC CALENDAR. This project collects no events at all. There is")
    a("  no NEXT IMPORTANT EVENT field, graded or otherwise. Its absence here is")
    a("  NOT evidence that nothing is scheduled — verify externally per mandate")
    a("  rule 1 before treating the window as event-free.")
    a("* NO ORDER BOOK. No depth, no bids, no asks, no liquidity walls, ever.")
    a("  Where a 'zones' factor appears it is built from this project's own")
    a("  support/resistance levels — past defended prices, not resting size.")
    a("  Do not describe them as walls.")
    a("* NO LONG/SHORT RATIO. Not collected on this feed.")
    a("")

    # -- CONTEXT -------------------------------------------------------------
    sess = ctx.get("session") or {}
    if sess:
        a("-" * 80)
        a("CONTEXT — read this before any conclusion (mandate rule 4)")
        a("-" * 80)
        a(f"last closed bar        {sess.get('bar_myt','-')} MYT "
          f"({sess.get('weekday_myt','-')})")
        a(f"session                {sess.get('label','-')}")
        a(f"gold market open       {_yn(sess.get('gold_market_open'))}")
        a(f"weekend (MYT)          {_yn(sess.get('is_weekend_myt'))}")
        a(f"timing rules apply     {_yn(sess.get('timing_rules_apply'))}"
          + ("" if sess.get("timing_rules_apply")
             else "   <-- the three session windows DO NOT apply"))
        fr = ctx.get("freshness") or {}
        if fr:
            a(f"feed age               {_dur(fr.get('bar_age_ms'))}"
              + ("   <-- STALE, everything below describes THEN" if fr.get("stale") else ""))
        a("")

    # -- session liquidity map -----------------------------------------------
    ses = ctx.get("sessions")
    if isinstance(ses, dict) and not ses.get("error"):
        a("-" * 80)
        a("SESSION MAP — Asia range / Europe sweeps / US confirmation")
        a("-" * 80)
        a("windows (MYT): asia 07:00-15:00 · europe 15:00-20:00 · us 20:00-05:00")
        a(f"phase                  {ses.get('phase','-')} · day "
          f"{ses.get('day_myt','-')}"
          + (" · WEEKEND" if ses.get("weekend") else ""))
        slv = ses.get("levels") or {}
        if slv.get("asia_high") is None:
            a("Asia range             no bars yet today — nothing to sweep")
        else:
            a(f"Asia high / low        {_n(slv.get('asia_high'))} / "
              f"{_n(slv.get('asia_low'))}"
              + ("" if slv.get("asia_done") else "   (still building)")
              + f"   range {_n(slv.get('asia_range'))}")
        a(f"prev day high / low    {_n(slv.get('prev_day_high'))} / "
          f"{_n(slv.get('prev_day_low'))}")
        a(f"prev US high / low     {_n(slv.get('prev_us_high'))} / "
          f"{_n(slv.get('prev_us_low'))}")
        a(f"day open               {_n(slv.get('day_open'))}")
        a(f"state                  {ses.get('state','-')}")
        b = ses.get("bias") or {}
        if isinstance(b, dict):
            lbl = {"long": "看多 LONG", "short": "看空 SHORT",
                   "range": "震荡 RANGE", "undecided": "未定 UNDECIDED"}\
                .get(b.get("bias"), str(b.get("bias")))
            when = {"asia": "Asia — range by definition",
                    "europe": "Europe statement — US verdict later"}.get(
                b.get("phase"),
                "DAY VERDICT (from " + str(b.get("verdict_myt", "20:30"))
                + " MYT)" if b.get("ready")
                else "US verdict at " + str(b.get("verdict_myt", "20:30")))
            a(f"日内倾向 (day bias)    {lbl}   [{when}]")
            a("  basis (facts the read comes from):")
            for line in (b.get("basis") or [])[:6]:
                a(f"    · {line}")
        us = ses.get("us")
        if us:
            a(f"US read                {us.get('verdict')} — Europe closed "
              f"{_n(us.get('europe_close'))},"
              f" US move {_n(us.get('move'))} vs threshold "
              f"{_n(us.get('threshold'))}")
        evs = ses.get("events") or []
        if evs:
            a("events today (sweep = poke that returned · break = 2 closes "
              "beyond ·")
            a("reclaim = a break that returned):")
            for e in evs[-8:]:
                v = "" if e.get("vol_ok") is None else \
                    f"   [volume {'confirmed' if e['vol_ok'] else 'DID NOT confirm'}]"
                a(f"  {e.get('ts_myt','-')}  {e.get('type','?'):<12} "
                  f"@ {_n(e.get('price'))} (level {_n(e.get('level'))}){v}")
        else:
            a("events today           none — the range has not been tested "
              "outside Asia")
        a("A sweep or break is STRUCTURE, not a direction. 突破等确认，扫盘不追单.")
        a("")

    # -- the engine's own scored inputs -------------------------------------
    adx = ctx.get("adx") or {}
    vol = ctx.get("volume") or {}
    if adx or vol:
        a("-" * 80)
        a("ENGINE SCORING INPUTS — obey these bands literally (mandate rules 1-3)")
        a("-" * 80)
        if adx:
            a(f"ADX now                {_n(adx.get('now'), 1)}")
            a(f"  last {len(adx.get('last_bars') or [])} bars           "
              f"{adx.get('last_bars')}")
            a(f"  slope                {_n(adx.get('slope'), 2)}"
              f"   ({'RISING' if adx.get('rising') else 'falling'})"
              if adx.get("slope") is not None else
              "  slope                unknown — read the single point only")
            a(f"  regime               {adx.get('regime','-')}")
        if vol:
            a(f"Tick volume            {_n(vol.get('relative_to_20bar'), 2)}x "
              f"the {vol.get('period', 20)}-bar average")
            a(f"  band                 {vol.get('band','-')}")
            a(f"  (bands: >=1.5 strong | >=1.2 participating | 0.8-1.2 "
              "neutral-weak | 0.5-0.8 weak | <0.5 very thin. NO 1.0 line.)")
            a(f"trend_strength         {_n(vol.get('trend_strength'), 1)} / 100 "
              f"({vol.get('trend_direction','-')})")
            if vol.get("participation_divergence"):
                a("  !! PARTICIPATION DIVERGENCE — strength is high, participation")
                a("     is not. The score is likely carried by structure (EMA, UT,")
                a("     multi-timeframe agreement), not by real volume.")
        if ctx.get("bb_percent_b") is not None:
            a(f"BB %B                  {_n(ctx.get('bb_percent_b'), 3)}")
        if ctx.get("atr_percent") is not None:
            a(f"ATR as % of price      {_n(ctx.get('atr_percent'), 3)}%")
        a("")

    al = ctx.get("alignment") or []
    if al:
        s = ctx.get("alignment_summary") or {}
        a("-" * 80)
        a("MULTI-TIMEFRAME ALIGNMENT")
        a("-" * 80)
        a(f"  {'tf':<5}{'bias':<8}{'UT':<5}{'EMA':<5}{'ADX':>6}{'%B':>8}{'vol':>7}")
        for r in al:
            bias = {1: "bull", -1: "bear"}.get(r.get("confirmed_bias"), "flat")
            a(f"  {r['tf']:<5}{bias:<8}{r.get('ut_bias',0):<5}"
              f"{r.get('ema_alignment',0):<5}{_n(r.get('adx'),1):>6}"
              f"{_n(r.get('percent_b'),2):>8}{_n(r.get('rel_volume'),2):>7}")
        a(f"  {s.get('bullish_tfs',0)} bullish / {s.get('bearish_tfs',0)} bearish / "
          f"{s.get('flat_tfs',0)} flat" + ("  — UNANIMOUS" if s.get("unanimous") else ""))
        a("")

    # -- funding -------------------------------------------------------------
    fu = ctx.get("funding") or {}
    if fu.get("available") is False:
        a("-" * 80)
        a(f"FUNDING: unavailable — {fu.get('why','')}")
        a("-" * 80)
        a("")
    elif fu:
        a("-" * 80)
        a("FUNDING — compared against the venue baseline (mandate rule 5)")
        a("-" * 80)
        a(f"rate                   {_n(fu.get('rate_pct'), 5)}%  "
          f"(as of {fu.get('as_of_myt','-')} MYT)")
        a(f"baseline               {_n(fu.get('baseline_pct'), 4)}%   "
          "<-- the venue's neutral; paying this is NOT crowding")
        a(f"deviation              {_n(fu.get('deviation_pct'), 5)}%  "
          f"(crowded beyond +/-{_n(fu.get('crowded_threshold_pct'), 4)}%)")
        a(f"VERDICT                {fu.get('verdict','-')}")
        a(f"recent                 {fu.get('recent_pct')}")
        a("")

    # -- open interest -------------------------------------------------------
    oi = ctx.get("open_interest") or {}
    if oi.get("available") is False:
        a("-" * 80)
        a("OPEN INTEREST: quadrant unavailable")
        a("-" * 80)
        a(f"  {oi.get('why','')}")
        a("")
    elif oi:
        a("-" * 80)
        a("OPEN INTEREST — four-quadrant read (mandate rule 6)")
        a("-" * 80)
        a(f"OI now                 {_n(oi.get('now'))}  "
          f"(as of {oi.get('as_of_myt','-')} MYT, {oi.get('interval')} grid)")
        a(f"OI previous            {_n(oi.get('prev'))}")
        a(f"delta OI               {_n(oi.get('delta'))}  "
          f"({_n(oi.get('delta_pct'), 3)}%)")
        a(f"delta price            {_n(oi.get('price_delta'))}  (same interval)")
        a(f"QUADRANT               {oi.get('quadrant','-')}")
        a("")

    # -- price / technical ---------------------------------------------------
    if meta or price is not None:
        a("-" * 80)
        a("PRICE AND TECHNICAL STATE")
        a("-" * 80)
        a(f"price now              {_n(px)}")
        if meta.get("last_closed_bar_ms"):
            a(f"last CLOSED bar        {_t(meta['last_closed_bar_ms'])} "
              f"({meta.get('anchor','?')})")
            a(f"  close                {_n(meta.get('last_closed_bar_close'))}")
        for key, label, dp in (("atr_14", "ATR 14", 3), ("rsi_14", "RSI 14", 1),
                               ("adx_14", "ADX 14", 1), ("ut_level", "UT level", 2)):
            if meta.get(key) is not None:
                a(f"{label:<22} {_n(meta[key], dp)}")
        a("  (UT level is an ATR TRAILING STOP, not a defended price.)")
        a("")

    # -- the risk read -------------------------------------------------------
    if risk and "error" not in risk:
        a("-" * 80)
        a("RISK READ — hand-set weights, never measured on gold")
        a("-" * 80)
        a(f"structural bias        {risk.get('bias','?')}")
        a(f"bracket assessed       TP {_n(risk.get('tp_pct'),3)}% / "
          f"SL {_n(risk.get('sl_pct'),3)}%")
        a(f"data note              {risk.get('data_confidence_note','')}")
        a("")
        for side in ("long", "short"):
            s = risk.get(side) or {}
            a(f"  {side.upper()}")
            a(f"    danger      {int(s.get('danger',0)):>3}%  [{_bar(s.get('danger'))}]")
            a(f"    confidence  {int(s.get('confidence',0)):>3}%  [{_bar(s.get('confidence'))}]")
            a(f"    factors     trend {s.get('trend_pts','?')}/40  "
              f"zones {s.get('orderbook_pts','?')}/30  "
              f"momentum {s.get('momentum_pts','?')}/30   (points of danger)")
            for r in s.get("reasons", []):
                mark = "!!" if r.get("polarity") == "DANGER" else "ok"
                a(f"      [{mark}] {r.get('factor','')}: {r.get('text','')}")
            a("")

        sim = risk.get("historical_sim")
        if sim and sim.get("matches"):
            a("  BARRIER STATISTICS (not a backtest — no entry rule, no fees, no")
            a("  sequencing; a conditional frequency over bars whose EMA28 distance")
            a("  and RSI zone match now, walked forward to the bracket)")
            a(f"    {sim.get('summary','')}")
            if sim.get("long_sl_first_prob") is not None:
                a(f"    long  P(stop first)  {float(sim['long_sl_first_prob'])*100:.1f}%")
            if sim.get("short_sl_first_prob") is not None:
                a(f"    short P(stop first)  {float(sim['short_sl_first_prob'])*100:.1f}%")
            a("    Ties (a bar touching both barriers) counted as STOP first.")
            a("")

        z = risk.get("zones") or {}
        if z.get("resistance") or z.get("support"):
            a("  KEY LEVELS  (swing / session / previous-day zones. NOT walls, and")
            a("  never derived from the UT level, which is a trailing stop)")
            for lo, hi, strength, src in (z.get("resistance") or []):
                width = float(hi) - float(lo)
                flag = "   <-- ZERO WIDTH" if width <= 0 else ""
                a(f"    R  {_n(lo)} - {_n(hi)}   strength {_n(strength,1)}   "
                  f"{', '.join(src)}{flag}")
            if px is not None:
                a(f"    -> price {_n(px)}")
            for lo, hi, strength, src in (z.get("support") or []):
                width = float(hi) - float(lo)
                flag = "   <-- ZERO WIDTH" if width <= 0 else ""
                a(f"    S  {_n(lo)} - {_n(hi)}   strength {_n(strength,1)}   "
                  f"{', '.join(src)}{flag}")
            if z.get("stale_ms"):
                a(f"    (zone card is on the H1 grid — {_dur(z['stale_ms'])} old)")
            a("")

    # -- the rule ------------------------------------------------------------
    if rule and "error" not in rule:
        a("-" * 80)
        a("THE MECHANICAL RULE — what the system itself decided")
        a("-" * 80)
        a(f"symbol / anchor        {rule.get('symbol')} {rule.get('anchor')} "
          f"({rule.get('side')} only)")
        a(f"bracket                {rule.get('bracket','')}")
        if rule.get("bracket_measured") is False:
            a("                       ^ NOT the measured bracket. The measured one")
            a("                         is TP 5.0 / SL 2.5 x ATR14; a symmetric")
            a("                         bracket measured as the cell that took a")
            a("                         $200 account to -$130 on the real sequence.")
        a(f"all legs true          {rule.get('fired')}")
        # NOT "a slot is free" — this is would_enter, which is (fired AND a slot
        # is free). When the legs have not fired it is False for that reason
        # alone, and labelling it as occupancy made a reader conclude the book
        # was full when the real answer was in reason_if_skipped.
        a(f"would open a trade     {rule.get('would_enter')}")
        a(f"SIGNAL SENT            {rule.get('sent')}")
        if rule.get("vetoed"):
            a("VETOED BY THE RISK GATE")
        if rule.get("reason"):
            a(f"reason                 {rule['reason']}")
        a("")
        a("  legs on the last closed bar:")
        for leg, ok in (rule.get("legs") or {}).items():
            a(f"    [{'x' if ok else ' '}] {leg}")
        if rule.get("blocking"):
            a(f"  blocking: {', '.join(rule['blocking'])}")
        a("")
        if rule.get("entry_ref"):
            a(f"  would enter at       {_n(rule['entry_ref'])} "
              "(reference; the fill is the NEXT bar's open)")
            a(f"  stop                 {_n(rule.get('sl'))}")
            a(f"  target               {_n(rule.get('tp'))}")
            a(f"  size                 {rule.get('lots')} lots")
            a(f"  time stop            {_t(rule.get('time_stop_ms'))}")
        g = rule.get("gate")
        if g:
            a("")
            a(f"  gate: {'PASS' if g.get('passed') else 'VETO'}  "
              f"danger {g.get('danger')}  confidence {g.get('confidence')}  "
              f"bias {g.get('bias')}")
            for v in g.get("vetoes") or []:
                a(f"    veto: {v}")
        a("")

    # -- measured evidence base ---------------------------------------------
    a("-" * 80)
    a("WHAT HAS ACTUALLY BEEN MEASURED (the evidence base for the rule above)")
    a("-" * 80)
    a("* The 8-leg entry, long-only, TP 5.0 / SL 2.5 x ATR14, 96-bar time stop:")
    a("  n=588 over 2022-05-30 -> 2026-08-21, 42.5% win (95% CI 38.6-46.5)")
    a("  against a 34.2% break-even. PF 1.38. First config whose CI lies")
    a("  entirely above break-even.")
    a("* Against a MATCHED RANDOM-ENTRY null (same panel, same session filter,")
    a("  same bracket, only the entry randomised, 300 replicates): the rule beats")
    a("  it by ~+3.3 points, consistently, at every bracket and on BOTH sides.")
    a("  That is the edge. It is small and it is real.")
    a("* 42.5% is NOT a bad win rate — break-even at that bracket is 34.2%.")
    a("  Reading a win rate without its bracket is the commonest error here.")
    a("* Expect long losing runs: 9 consecutive losses is in the real record.")
    a("* Shorts beat their own null by +3.0 but still lose money: a symmetric")
    a("  short pays a -3.4 drift tax on an instrument that went 1,800 -> 4,300.")
    a("  Long-only is a drift decision, not a skill one.")
    a("* SIX mined filters have already been TESTED AND REJECTED. Two died for")
    a("  the same structural reason: they were true at the moment the entry")
    a("  fired, so they could not discriminate. Before proposing a filter, ask")
    a("  whether it can be false at the moment the entry fires.")
    a("* The search budget on these columns is spent (4,000+ conditions over 496")
    a("  columns). More mining on the same data is not informative.")
    a("")

    # -- positions -----------------------------------------------------------
    if positions:
        a("-" * 80)
        a("OPEN POSITIONS AT THE BROKER (the owner's own, all symbols)")
        a("-" * 80)
        for p in positions:
            a(f"  {p.get('broker_symbol') or p.get('symbol')} "
              f"{str(p.get('side','')).upper()} {p.get('volume')} lots  "
              f"opened {_t(p.get('open_time'))} @ {_n(p.get('open_price'))}")
            sl, tp = p.get("sl"), p.get("tp")
            a(f"      stop {_n(sl) if sl else 'NONE  <-- a position with no stop'}"
              f"   target {_n(tp) if tp else 'none'}")
        a("")
    elif positions is not None:
        a("-" * 80)
        a("OPEN POSITIONS AT THE BROKER: none")
        a("-" * 80)
        a("")

    # -- recent signals ------------------------------------------------------
    if events:
        a("-" * 80)
        a(f"RECENT SIGNALS EMITTED ({len(events)}, newest last)")
        a("-" * 80)
        for e in events[-12:]:
            g = e.get("gate") or {}
            a(f"  {_t(e.get('bar_open_ms'))}  {str(e.get('side','')).upper():<5} "
              f"@ {_n(e.get('entry_ref'))}  SL {_n(e.get('sl'))}  TP {_n(e.get('tp'))}"
              f"  danger {g.get('danger','-')}")
        a("")

    a(HOW_TO_READ)
    a("=" * 80)
    a("Deeper analysis is possible directly against the database:")
    a("  backtest/data/market.db  (sqlite)")
    a("    candles(symbol, interval, open_time, open, high, low, close, volume)")
    a("    open_interest(symbol, interval, ts, oi)")
    a("    funding(symbol, funding_time, rate)")
    a("    broker_trades(...)     the owner's real MT5 fills, entry and exit")
    a("    zone_snapshots(...)    the KEY LEVELS card per H1 bar")
    a("  backtest/reports/signals.jsonl   every evaluation, fired or not,")
    a("                                   with the gate's verdict in `extras`")
    a("=" * 80)
    return "\n".join(L)


def build_json(**kw) -> str:
    """The same state as JSON, for a model that would rather parse than read."""
    return json.dumps({k: v for k, v in kw.items() if v is not None},
                      indent=1, default=str, ensure_ascii=False)
