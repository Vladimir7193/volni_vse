from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Any
import pandas as pd
from loguru import logger

from config import (
    MIN_RR_RATIO,
    MAX_STOP_LOSS_PCT,
    MIN_MOVE_PCT,
)
from indicators import calculate_atr
from trendline import TrendlineAnalyzer
from wave_analyzer import WaveAnalyzer
from risk_manager import RiskManager


BREAKOUT_CONFIRMATION_MODE = "close"
TP1_R_MULTIPLE = 1.5
TP2_R_MULTIPLE = 3.0
SENIOR_ALIGNMENT_BONUS = 8
BROKEN_TRENDLINE_BONUS = 10
ATR_STOP_BUFFER_MULTIPLIER = 0.5
MAX_BREAKOUT_DISTANCE_PCT = 0.7
MIN_VOLUME_SURGE_MULTIPLIER = 1.2


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _quality_from_score(score: float) -> str:
    if score >= 70:
        return "A+"
    if score >= 55:
        return "A"
    if score >= 40:
        return "B"
    return "C"


@dataclass(slots=True)
class SetupFeatures:
    impulse_size_pct: float = 0.0
    correction_size_pct: float = 0.0
    stop_loss_pct: float = 0.0
    rr_tp1: float = 0.0
    rr_tp2: float = 0.0
    senior_alignment: int = 0
    trendline_broken: bool = False
    breakout_distance_pct: float = 0.0
    atr_pct: float = 0.0
    confidence_score: float = 0.0
    volume_surge: float = 0.0


@dataclass(slots=True)
class TradeSetup:
    symbol: str
    timeframe: str
    direction: str
    setup_type: str

    entry_price: float
    stop_loss: float
    tp1_price: float
    tp2_price: float
    position_size_pct: float

    quality: str
    score: float

    trendline_broken: bool
    breakout_level: float

    is_valid: bool
    invalid_reason: str = ""

    current_price: float = 0.0
    senior_trend: str = "neutral"

    impulse_legs_count: int = 0
    correction_legs_count: int = 0

    rr_ratio: float = 0.0
    stop_loss_pct: float = 0.0
    expected_move_pct: float = 0.0

    features: SetupFeatures = field(default_factory=SetupFeatures)
    meta: dict = field(default_factory=dict)


class SetupDetector:
    def __init__(self) -> None:
        self.wave_analyzer = WaveAnalyzer()
        self.trendline_analyzer = TrendlineAnalyzer()
        self.risk_manager = RiskManager()

    def scan_for_setups(
        self,
        symbol: str,
        df_working: pd.DataFrame,
        timeframe_working: str,
        df_senior: Optional[pd.DataFrame],
        timeframe_senior: str,
        current_price: float,
    ) -> list[TradeSetup]:
        setups: list[TradeSetup] = []

        try:
            if df_working is None or len(df_working) < 50:
                return setups

            current_price = _safe_float(current_price)
            if current_price <= 0:
                return setups

            context = self._build_context(
                df_senior=df_senior,
                timeframe_senior=timeframe_senior,
            )

            setup_1 = self._detect_impulse_pullback_breakout(
                symbol=symbol,
                df=df_working,
                timeframe=timeframe_working,
                current_price=current_price,
                context=context,
            )
            if setup_1:
                setups.append(setup_1)

            setup_2 = self._detect_flag_breakout(
                symbol=symbol,
                df=df_working,
                timeframe=timeframe_working,
                current_price=current_price,
                context=context,
            )
            if setup_2:
                setups.append(setup_2)

        except Exception as exc:
            logger.exception("scan_for_setups failed for {} {}: {}", symbol, timeframe_working, exc)

        return setups

    def _detect_impulse_pullback_breakout(
        self,
        symbol: str,
        df: pd.DataFrame,
        timeframe: str,
        current_price: float,
        context: dict,
    ) -> Optional[TradeSetup]:
        impulse = self._extract_best_impulse(df)
        if not impulse:
            return None

        direction = impulse["direction"]
        impulse_start = _safe_float(impulse["start_price"])
        impulse_end = _safe_float(impulse["end_price"])
        impulse_size_pct = self._move_pct(impulse_start, impulse_end)

        if impulse_size_pct < MIN_MOVE_PCT:
            return None

        trendline_info = self._build_trendline_breakout(
            df=df,
            direction=direction,
            current_price=current_price,
        )
        if not trendline_info:
            return None

        breakout_level = trendline_info["breakout_level"]
        trendline_broken = trendline_info["trendline_broken"]

        stop_loss = self._derive_stop_from_structure(
            df=df,
            direction=direction,
            fallback_breakout=breakout_level,
        )
        if stop_loss <= 0:
            return None

        entry_price = self._derive_entry_price(
            current_price=current_price,
            breakout_level=breakout_level,
            trendline_broken=trendline_broken,
        )

        plan = self._build_execution_plan(
            entry_price=entry_price,
            stop_loss=stop_loss,
            direction=direction,
            setup_quality_hint="A" if trendline_broken else "B",
        )
        if not plan:
            return None

        features = self._build_features(
            df=df,
            direction=direction,
            current_price=current_price,
            entry_price=plan["entry_price"],
            stop_loss=plan["stop_loss"],
            tp1_price=plan["tp1_price"],
            tp2_price=plan["tp2_price"],
            trendline_broken=trendline_broken,
            breakout_level=breakout_level,
            impulse_size_pct=impulse_size_pct,
            context=context,
            correction_size_pct=_safe_float(impulse.get("correction_size_pct"), 0.0),
        )

        score = self._score_setup(features, setup_type="setup_1")
        quality = _quality_from_score(score)

        position_size_pct = self.risk_manager.calculate_position_size(
            entry_price=plan["entry_price"],
            stop_loss=plan["stop_loss"],
            setup_quality=quality,
        )

        setup = TradeSetup(
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            setup_type="setup_1",
            entry_price=plan["entry_price"],
            stop_loss=plan["stop_loss"],
            tp1_price=plan["tp1_price"],
            tp2_price=plan["tp2_price"],
            position_size_pct=position_size_pct,
            quality=quality,
            score=score,
            trendline_broken=trendline_broken,
            breakout_level=breakout_level,
            is_valid=True,
            current_price=current_price,
            senior_trend=context.get("senior_trend", "neutral"),
            impulse_legs_count=int(impulse.get("legs_count", 5)),
            correction_legs_count=int(impulse.get("correction_legs_count", 3)),
            rr_ratio=features.rr_tp2,
            stop_loss_pct=features.stop_loss_pct,
            expected_move_pct=features.impulse_size_pct,
            features=features,
            meta={
                "pattern_family": "impulse_pullback_breakout",
                "breakout_confirmation_mode": BREAKOUT_CONFIRMATION_MODE,
            },
        )
        return self._final_validate(setup)

    def _detect_flag_breakout(
        self,
        symbol: str,
        df: pd.DataFrame,
        timeframe: str,
        current_price: float,
        context: dict,
    ) -> Optional[TradeSetup]:
        flag = self._extract_flag_pattern(df)
        if not flag:
            return None

        direction = flag["direction"]
        breakout_level = _safe_float(flag["breakout_level"])
        trendline_broken = self._is_breakout_confirmed(
            current_price=current_price,
            breakout_level=breakout_level,
            direction=direction,
            df=df,
        )

        entry_price = self._derive_entry_price(
            current_price=current_price,
            breakout_level=breakout_level,
            trendline_broken=trendline_broken,
        )

        stop_loss = _safe_float(flag["stop_loss"])
        if stop_loss <= 0:
            return None

        plan = self._build_execution_plan(
            entry_price=entry_price,
            stop_loss=stop_loss,
            direction=direction,
            setup_quality_hint="A" if trendline_broken else "B",
        )
        if not plan:
            return None

        features = self._build_features(
            df=df,
            direction=direction,
            current_price=current_price,
            entry_price=plan["entry_price"],
            stop_loss=plan["stop_loss"],
            tp1_price=plan["tp1_price"],
            tp2_price=plan["tp2_price"],
            trendline_broken=trendline_broken,
            breakout_level=breakout_level,
            impulse_size_pct=_safe_float(flag.get("impulse_size_pct"), 0.0),
            context=context,
            correction_size_pct=_safe_float(flag.get("flag_depth_pct"), 0.0),
        )

        score = self._score_setup(features, setup_type="setup_2")
        quality = _quality_from_score(score)

        position_size_pct = self.risk_manager.calculate_position_size(
            entry_price=plan["entry_price"],
            stop_loss=plan["stop_loss"],
            setup_quality=quality,
        )

        setup = TradeSetup(
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            setup_type="setup_2",
            entry_price=plan["entry_price"],
            stop_loss=plan["stop_loss"],
            tp1_price=plan["tp1_price"],
            tp2_price=plan["tp2_price"],
            position_size_pct=position_size_pct,
            quality=quality,
            score=score,
            trendline_broken=trendline_broken,
            breakout_level=breakout_level,
            is_valid=True,
            current_price=current_price,
            senior_trend=context.get("senior_trend", "neutral"),
            impulse_legs_count=3,
            correction_legs_count=3,
            rr_ratio=features.rr_tp2,
            stop_loss_pct=features.stop_loss_pct,
            expected_move_pct=features.impulse_size_pct,
            features=features,
            meta={
                "pattern_family": "flag_breakout",
                "breakout_confirmation_mode": BREAKOUT_CONFIRMATION_MODE,
            },
        )
        return self._final_validate(setup)

    def _build_context(
        self,
        df_senior: Optional[pd.DataFrame],
        timeframe_senior: str,
    ) -> dict:
        if df_senior is None or len(df_senior) < 50:
            return {
                "senior_trend": "neutral",
                "timeframe_senior": timeframe_senior,
            }

        try:
            analysis = self.wave_analyzer.analyze_context(df_senior)
            if isinstance(analysis, dict):
                return {
                    "senior_trend": analysis.get("trend", "neutral"),
                    "timeframe_senior": timeframe_senior,
                    "context_raw": analysis,
                }
        except Exception as exc:
            logger.debug("Senior context fallback due to error: {}", exc)

        return {
            "senior_trend": "neutral",
            "timeframe_senior": timeframe_senior,
        }

    def _build_features(
        self,
        df: pd.DataFrame,
        direction: str,
        current_price: float,
        entry_price: float,
        stop_loss: float,
        tp1_price: float,
        tp2_price: float,
        trendline_broken: bool,
        breakout_level: float,
        impulse_size_pct: float,
        context: dict,
        correction_size_pct: float,
    ) -> SetupFeatures:
        atr_pct = self._atr_pct(df, current_price)
        stop_loss_pct = self._move_pct(entry_price, stop_loss)
        breakout_distance_pct = self._move_pct(current_price, breakout_level)
        rr_tp1 = self._rr(entry_price, stop_loss, tp1_price)
        rr_tp2 = self._rr(entry_price, stop_loss, tp2_price)

        senior_alignment = 0
        senior_trend = context.get("senior_trend", "neutral")
        if (senior_trend == "bullish" and direction == "long") or (
            senior_trend == "bearish" and direction == "short"
        ):
            senior_alignment = 1

        volume_surge = self._volume_surge_ratio(df)

        return SetupFeatures(
            impulse_size_pct=_safe_float(impulse_size_pct),
            correction_size_pct=_safe_float(correction_size_pct),
            stop_loss_pct=_safe_float(stop_loss_pct),
            rr_tp1=_safe_float(rr_tp1),
            rr_tp2=_safe_float(rr_tp2),
            senior_alignment=senior_alignment,
            trendline_broken=trendline_broken,
            breakout_distance_pct=_safe_float(breakout_distance_pct),
            atr_pct=_safe_float(atr_pct),
            confidence_score=0.0,
            volume_surge=_safe_float(volume_surge),
        )

    def _score_setup(self, features: SetupFeatures, setup_type: str) -> float:
        score = 0.0

        score += _clamp((features.rr_tp2 - 1.0) * 14.0, 0.0, 28.0)
        score += _clamp(features.impulse_size_pct * 1.0, 0.0, 12.0)

        if 0 < features.stop_loss_pct <= MAX_STOP_LOSS_PCT:
            score += 10.0
        elif features.stop_loss_pct > MAX_STOP_LOSS_PCT:
            score -= 20.0

        if features.senior_alignment:
            score += SENIOR_ALIGNMENT_BONUS

        if features.trendline_broken:
            score += BROKEN_TRENDLINE_BONUS

        if features.atr_pct > 0:
            if 0.35 <= features.atr_pct <= 3.5:
                score += 6.0
            else:
                score -= 4.0

        if features.breakout_distance_pct > MAX_BREAKOUT_DISTANCE_PCT:
            score -= 8.0

        if features.volume_surge >= MIN_VOLUME_SURGE_MULTIPLIER:
            score += 8.0
        else:
            score -= 6.0

        if setup_type == "setup_1":
            score += 4.0
        elif setup_type == "setup_2":
            score += 2.0

        return _clamp(round(score, 2), 0.0, 100.0)

    def _build_execution_plan(
        self,
        entry_price: float,
        stop_loss: float,
        direction: str,
        setup_quality_hint: str,
    ) -> Optional[dict]:
        if entry_price <= 0 or stop_loss <= 0:
            return None

        stop_loss_pct = self._move_pct(entry_price, stop_loss)
        if stop_loss_pct <= 0:
            return None

        risk_distance = abs(entry_price - stop_loss)

        if direction == "long":
            tp1_price = entry_price + risk_distance * TP1_R_MULTIPLE
            tp2_price = entry_price + risk_distance * TP2_R_MULTIPLE
        else:
            tp1_price = entry_price - risk_distance * TP1_R_MULTIPLE
            tp2_price = entry_price - risk_distance * TP2_R_MULTIPLE

        rr_tp2 = self._rr(entry_price, stop_loss, tp2_price)
        if rr_tp2 < MIN_RR_RATIO:
            return None

        return {
            "entry_price": _safe_float(entry_price),
            "stop_loss": _safe_float(stop_loss),
            "tp1_price": _safe_float(tp1_price),
            "tp2_price": _safe_float(tp2_price),
            "quality_hint": setup_quality_hint,
        }

    def _final_validate(self, setup: TradeSetup) -> TradeSetup:
        reason = ""

        if setup.entry_price <= 0 or setup.stop_loss <= 0:
            reason = "invalid_entry_or_stop"
        elif setup.tp1_price <= 0 or setup.tp2_price <= 0:
            reason = "invalid_take_profit"
        elif setup.position_size_pct <= 0:
            reason = "zero_position_size"
        elif setup.stop_loss_pct > MAX_STOP_LOSS_PCT:
            reason = f"stop_too_wide>{MAX_STOP_LOSS_PCT}"
        elif setup.rr_ratio < MIN_RR_RATIO:
            reason = f"rr_below_min<{MIN_RR_RATIO}"
        elif setup.expected_move_pct < MIN_MOVE_PCT and setup.setup_type == "setup_1":
            reason = f"move_below_min<{MIN_MOVE_PCT}"

        if reason:
            setup.is_valid = False
            setup.invalid_reason = reason
        else:
            setup.is_valid = True
            setup.invalid_reason = ""

        return setup

    def _extract_best_impulse(self, df: pd.DataFrame) -> Optional[dict]:
        try:
            waves = self.wave_analyzer.find_impulse_waves(df)
            if not waves:
                return None

            best = max(waves, key=lambda x: _safe_float(x.get("score"), 0.0))
            return {
                "direction": best.get("direction", "long"),
                "start_price": _safe_float(best.get("wave1_start_price")),
                "end_price": _safe_float(best.get("wave5_end_price")),
                "score": _safe_float(best.get("score")),
                "legs_count": 5,
                "correction_legs_count": 3,
                "correction_size_pct": _safe_float(best.get("wave4_depth_pct"), 0.0),
                "raw": best,
            }
        except Exception as exc:
            logger.debug("Impulse extraction failed: {}", exc)
            return None

    def _extract_flag_pattern(self, df: pd.DataFrame) -> Optional[dict]:
        try:
            trendline_data = self.trendline_analyzer.find_flag_pattern(df)
            if not trendline_data:
                return None

            direction = trendline_data.get("direction", "long")
            breakout_level = _safe_float(trendline_data.get("breakout_level"))
            stop_loss = _safe_float(trendline_data.get("stop_loss"))

            if breakout_level <= 0 or stop_loss <= 0:
                return None

            return {
                "direction": direction,
                "breakout_level": breakout_level,
                "stop_loss": stop_loss,
                "impulse_size_pct": _safe_float(trendline_data.get("impulse_size_pct"), 0.0),
                "flag_depth_pct": _safe_float(trendline_data.get("flag_depth_pct"), 0.0),
                "raw": trendline_data,
            }
        except Exception as exc:
            logger.debug("Flag extraction failed: {}", exc)
            return None

    def _build_trendline_breakout(
        self,
        df: pd.DataFrame,
        direction: str,
        current_price: float,
    ) -> Optional[dict]:
        try:
            trendline = self.trendline_analyzer.build_breakout_line(df, direction=direction)
            if not trendline:
                return None

            breakout_level = _safe_float(trendline.get("breakout_level"))
            if breakout_level <= 0:
                return None

            return {
                "breakout_level": breakout_level,
                "trendline_broken": self._is_breakout_confirmed(
                    current_price=current_price,
                    breakout_level=breakout_level,
                    direction=direction,
                    df=df,
                ),
                "raw": trendline,
            }
        except Exception as exc:
            logger.debug("Trendline breakout build failed: {}", exc)
            return None

    def _derive_stop_from_structure(
        self,
        df: pd.DataFrame,
        direction: str,
        fallback_breakout: float,
    ) -> float:
        if df is None or df.empty:
            return 0.0

        lookback = min(12, len(df))
        window = df.iloc[-lookback:]
        atr_value = self._atr_value(df)

        if direction == "long":
            level = _safe_float(window["low"].min(), 0.0)
            if level <= 0:
                level = fallback_breakout * 0.985
            if atr_value > 0:
                level -= atr_value * ATR_STOP_BUFFER_MULTIPLIER
        else:
            level = _safe_float(window["high"].max(), 0.0)
            if level <= 0:
                level = fallback_breakout * 1.015
            if atr_value > 0:
                level += atr_value * ATR_STOP_BUFFER_MULTIPLIER

        return max(_safe_float(level), 0.0)

    def _derive_entry_price(
        self,
        current_price: float,
        breakout_level: float,
        trendline_broken: bool,
    ) -> float:
        if trendline_broken:
            return _safe_float(current_price)
        return _safe_float(breakout_level)

    def _is_breakout_confirmed(
        self,
        current_price: float,
        breakout_level: float,
        direction: str,
        df: pd.DataFrame,
    ) -> bool:
        if breakout_level <= 0 or current_price <= 0:
            return False

        if BREAKOUT_CONFIRMATION_MODE == "intrabar":
            if direction == "long":
                return current_price > breakout_level
            return current_price < breakout_level

        if df is None or df.empty:
            return False

        last_close = _safe_float(df["close"].iloc[-1], 0.0)
        if direction == "long":
            return last_close > breakout_level
        return last_close < breakout_level

    def _atr_value(self, df: pd.DataFrame, period: int = 14) -> float:
        if df is None or len(df) < period + 2:
            return 0.0
        try:
            atr = calculate_atr(df, period=period)
            if isinstance(atr, pd.Series):
                return _safe_float(atr.iloc[-1], 0.0)
            return _safe_float(atr, 0.0)
        except Exception:
            return 0.0

    def _atr_pct(self, df: pd.DataFrame, price: float, period: int = 14) -> float:
        if df is None or len(df) < period + 2 or price <= 0:
            return 0.0
        value = self._atr_value(df, period=period)
        return (value / price) * 100.0 if value > 0 else 0.0

    def _volume_surge_ratio(self, df: pd.DataFrame, period: int = 20) -> float:
        if df is None or len(df) < period + 1 or "volume" not in df.columns:
            return 0.0
        try:
            current_vol = _safe_float(df["volume"].iloc[-1], 0.0)
            avg_vol = _safe_float(df["volume"].rolling(period).mean().iloc[-2], 0.0)
            if avg_vol <= 0:
                return 0.0
            return current_vol / avg_vol
        except Exception:
            return 0.0

    def _rr(self, entry: float, stop: float, target: float) -> float:
        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk <= 0:
            return 0.0
        return reward / risk

    def _move_pct(self, p1: float, p2: float) -> float:
        if p1 <= 0 or p2 <= 0:
            return 0.0
        return abs((p2 - p1) / p1) * 100.0