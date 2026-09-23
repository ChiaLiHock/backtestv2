"""Signal 7: the ExpD gate as a live channel.

The two properties this file pins:

* **Train/serve parity** — the live feature computation calls the SAME
  `features_for_break` the model was trained on, so a feature cannot mean
  something different at runtime. Tested bit-for-bit against
  `build_samples` on identical data.
* **Honest degradation** — no model artifact, no channel; and the gate
  never fires an event twice.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from backtest.engine.signal7 import Signal7, Signal7Config
from backtest.tests.test_signal6_retrain import _break_dataset
from backtest.tests.test_session_map import TUE_MYT0, H


def _seed(tmp_path, df):
    from backtest.data.db import CandleRepository, Database
    dbp = tmp_path / "s7.db"
    with Database(dbp) as db:
        CandleRepository(db).upsert("XAUUSDT", "1m", [
            tuple(r) for r in df[["open_time", "open", "high", "low",
                                  "close", "volume", "turnover"]].to_numpy()])
    return dbp


def _tiny_model(path, keys, bias=0.0, coef=1.0, thr=0.5):
    """A minimal deterministic artifact: P(win) = sigmoid(coef*x + bias)."""
    blob = {
        "keys": list(keys), "w": [bias] + [coef] * len(keys),
        "mu": [0.0] * len(keys), "sd": [1.0] * len(keys),
        "thresholds": {"trend": thr, "range": thr},
        "trained_at": 1, "train_n": 1, "train_feed": "test",
    }
    path.write_text(json.dumps(blob), encoding="utf-8")


def _block_for(df, as_of):
    from backtest.engine.session_map import build_map
    b = build_map(df, as_of_ms=as_of)
    b["symbol"] = "XAUUSDT"
    return b


class TestParity:
    def test_live_features_equal_training_features(self, tmp_path):
        """The crown-jewel property: identical inputs, identical numbers."""
        from backtest.data.db import Database
        from backtest.engine.signal6_retrain import FEATURE_KEYS, build_samples

        dbp = _seed(tmp_path, _break_dataset())
        with Database(dbp) as db:
            hist = build_samples(db, "XAUUSDT")
            s7 = Signal7(log_path=tmp_path / "s7.jsonl",
                         model_path=tmp_path / "none.json")
            block = _block_for(_break_dataset(), TUE_MYT0 + 18 * H)
            frame, path = s7._frame(db)
            from backtest.engine.session_map import myt_day_ms
            from backtest.engine.signal6_retrain import features_for_break
            day0 = myt_day_ms(TUE_MYT0 + 18 * H)
            rp = s7._range_pct(path, day0)
            ev = next(e for e in block["events"]
                      if e["type"] == "break_high")
            import numpy as np
            pt = path["open_time"].to_numpy(dtype="int64")
            m = (pt >= day0 - 86_400_000) & (pt < day0)
            dir_ref = float(path["open"].to_numpy()[int(np.argmax(m))])
            got = features_for_break(frame, ev, block["levels"],
                                     float(block["levels"]["asia_high"])
                                     - float(block["levels"]["asia_low"]),
                                     rp, day0, dir_ref, block["events"])
        assert got is not None
        feat, _aux = got
        assert len(hist) == 1
        for k in FEATURE_KEYS:
            a, b = hist[0][k], feat[k]
            assert (a == b) or (a != a and b != b) or abs(a - b) < 1e-9, k


class TestGate:
    def _setup(self, tmp_path, coef):
        dbp = _seed(tmp_path, _break_dataset())
        _tiny_model(tmp_path / "m.json",
                    ("prior_sweep", "vol_ok"), coef=coef)
        s7 = Signal7(log_path=tmp_path / "s7.jsonl",
                     model_path=tmp_path / "m.json")
        return dbp, s7

    def test_pass_when_p_beats_threshold(self, tmp_path):
        from backtest.data.db import Database
        dbp, s7 = self._setup(tmp_path, coef=5.0)   # all-positive -> p~1
        with Database(dbp) as db:
            out = s7.evaluate(db, _block_for(_break_dataset(),
                                             TUE_MYT0 + 18 * H), spot=4608.0)
        assert out["fired"] is True
        assert out["side"] == "long"
        assert out["p_win"] >= out["threshold"]
        assert out["sl"] < out["entry_ref"] < out["tp"]
        assert len(s7.events()) == 1
        assert s7.events()[0]["id"].startswith("s7:XAUUSDT@")

    def test_reject_when_p_below_threshold(self, tmp_path):
        from backtest.data.db import Database
        dbp, s7 = self._setup(tmp_path, coef=-5.0)  # all-negative -> p~0
        with Database(dbp) as db:
            out = s7.evaluate(db, _block_for(_break_dataset(),
                                             TUE_MYT0 + 18 * H), spot=4608.0)
        assert out["fired"] is False
        assert "reject" in out["reason"]
        assert s7.events() == []

    def test_no_artifact_means_quiet_channel(self, tmp_path):
        from backtest.data.db import Database
        dbp = _seed(tmp_path, _break_dataset())
        s7 = Signal7(log_path=tmp_path / "s7.jsonl",
                     model_path=tmp_path / "absent.json")
        with Database(dbp) as db:
            out = s7.evaluate(db, _block_for(_break_dataset(),
                                             TUE_MYT0 + 18 * H))
        assert out["fired"] is False
        assert "fit_signal7" in out["reason"]

    def test_fires_once_then_dedupes(self, tmp_path):
        from backtest.data.db import Database
        dbp, s7 = self._setup(tmp_path, coef=5.0)
        block = _block_for(_break_dataset(), TUE_MYT0 + 18 * H)
        with Database(dbp) as db:
            a = s7.evaluate(db, block, spot=4608.0)
            b = s7.evaluate(db, block, spot=4609.0)
        assert a["fired"] and not b["fired"]
        assert len(s7.events()) == 1
        assert s7.write(a) is True and s7.write(b) is False

    def test_seed_from_log_absorbs(self, tmp_path):
        from backtest.data.db import Database
        dbp, s7 = self._setup(tmp_path, coef=5.0)
        block = _block_for(_break_dataset(), TUE_MYT0 + 18 * H)
        with Database(dbp) as db:
            fired = s7.evaluate(db, block, spot=4608.0)
        s7.write(fired)
        fresh = Signal7(log_path=s7.log_path, model_path=tmp_path / "m.json")
        fresh.seed_from_log()
        with Database(dbp) as db:
            assert fresh.evaluate(db, block, spot=4608.0)["fired"] is False

    def test_config_declares_itself_unmeasured(self):
        assert Signal7Config().measured is False
        assert "experimental" in Signal7Config().label.lower()


class TestConditions:
    """The gate's visible conditions: signed per-feature contributions."""

    def _model_with_known_weights(self, tmp_path):
        """prior_sweep weight +2, vol_ok weight -1: contributions must be
        exactly z*weight and P must equal sigmoid of their sum."""
        keys = ("prior_sweep", "vol_ok")
        blob = {"keys": list(keys),
                "w": [0.0, 2.0, -1.0],
                "mu": [0.0, 0.0], "sd": [1.0, 1.0],
                "thresholds": {"trend": 0.5, "range": 0.5},
                "trained_at": 1, "train_n": 1, "train_feed": "test"}
        p = tmp_path / "m2.json"
        p.write_text(json.dumps(blob), encoding="utf-8")
        from backtest.engine.signal7 import _Model
        return _Model(json.loads(p.read_text(encoding="utf-8")))

    def test_contributions_are_exact_and_ordered(self, tmp_path):
        m = self._model_with_known_weights(tmp_path)
        p, contribs = m.predict_contrib({"prior_sweep": 1.0,
                                         "vol_ok": 1.0})
        d = dict(contribs)
        assert d["prior_sweep"] == 2.0 and d["vol_ok"] == -1.0
        import math
        assert p == pytest.approx(1.0 / (1.0 + math.exp(-1.0)))
        # ordered by |contribution|
        assert contribs[0][0] == "prior_sweep"

    def test_rejection_carries_its_conditions(self, tmp_path):
        from backtest.data.db import Database
        dbp = _seed(tmp_path, _break_dataset())
        _tiny_model(tmp_path / "m.json",
                    ("prior_sweep", "vol_ok"), coef=-5.0)
        s7 = Signal7(log_path=tmp_path / "s7.jsonl",
                     model_path=tmp_path / "m.json")
        with Database(dbp) as db:
            out = s7.evaluate(db, _block_for(_break_dataset(),
                                             TUE_MYT0 + 18 * H), spot=4608.0)
        assert out["fired"] is False
        assert out["p_win"] is not None and out["threshold"] is not None
        assert out["regime"] in ("trend", "range")
        assert out["contributions"], "the reject says WHICH features decided"
        assert "gate rejecting" in out["reason"]

    def test_firing_carries_conditions_too(self, tmp_path):
        from backtest.data.db import Database
        dbp = _seed(tmp_path, _break_dataset())
        _tiny_model(tmp_path / "m.json",
                    ("prior_sweep", "vol_ok"), coef=5.0)
        s7 = Signal7(log_path=tmp_path / "s7.jsonl",
                     model_path=tmp_path / "m.json")
        with Database(dbp) as db:
            out = s7.evaluate(db, _block_for(_break_dataset(),
                                             TUE_MYT0 + 18 * H), spot=4608.0)
        assert out["fired"] is True and out["contributions"]
