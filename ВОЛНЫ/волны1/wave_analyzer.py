"""
wave_analyzer.py — heuristic wave/context analyzer for scanner pipeline.
"""

from __future__ import annotations

from typing import Any, Optional
import math

import pandas as pd
from loguru import logger


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class WaveAnalyzer:
    def __init__(self, pivot_window: int = 3) -> None:
        self.pivot_window = pivot_window

    def find_impulse_waves(self, df: pd.DataFrame) -> list[dict]:
        if df is None or len(df) < 60:
            return []

        try:
            pivots = self._extract_pivots(df)
            if len(pivots) < 6:
                return []

            candidates: list[dict] = []

            for i in range(len(pivots) - 5):
                seq = pivots[i:i + 6]
                wave = self._build_wave_candidate(seq)
                if wave:
                    candidates.append(wave)

            candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
            return candidates[:10]

        except Exception as exc:
            logger.debug("find_impulse_waves failed: {}", exc)
            return []

    def analyze_context(self, df: pd.DataFrame) -> dict:
        if df is None or len(df) < 50:
            return {"trend": "neutral", "score": 0.0}

        try:
            closes = df["close"]
            ema_fast = closes.ewm(span=20, adjust=False).mean().iloc[-1]
            ema_slow = closes.ewm(span=50, adjust=False).mean().iloc[-1]
            last_close = float(closes.iloc[-1])

            bullish = last_close > ema_fast > ema_slow
            bearish = last_close < ema_fast < ema_slow

            impulse_candidates = self.find_impulse_waves(df)

            if bullish:
                trend = "bullish"
                score = 65.0
            elif bearish:
                trend = "bearish"
                score = 65.0
            else:
                trend = "neutral"
                score = 45.0

            if impulse_candidates:
                best = impulse_candidates[0]
                if best["direction"] == "long" and trend == "bullish":
                    score += 10.0
                elif best["direction"] == "short" and trend == "bearish":
                    score += 10.0
                else:
                    score -= 5.0

            return {
                "trend": trend,
                "score": round(score, 2),
                "ema_fast": float(ema_fast),
                "ema_slow": float(ema_slow),
                "last_close": float(last_close),
            }

        except Exception as exc:
            logger.debug("analyze_context failed: {}", exc)
            return {"trend": "neutral", "score": 0.0}

    # ------------------------------------------------------------------
    # Internal wave heuristics
    # ------------------------------------------------------------------

    def _extract_pivots(self, df: pd.DataFrame) -> list[tuple[int, float, str]]:
        highs = df["high"].tolist()
        lows = df["low"].tolist()
        w = self.pivot_window
        pivots: list[tuple[int, float, str]] = []

        for i in range(w, len(df) - w):
            hi = highs[i]
            lo = lows[i]

            if hi >= max(highs[i - w:i]) and hi >= max(highs[i + 1:i + 1 + w]):
                pivots.append((i, float(hi), "high"))

            if lo <= min(lows[i - w:i]) and lo <= min(lows[i + 1:i + 1 + w]):
                pivots.append((i, float(lo), "low"))

        pivots.sort(key=lambda x: x[0])
        return self._deduplicate_neighbor_pivots(pivots)

    def _deduplicate_neighbor_pivots(self, pivots: list[tuple[int, float, str]]) -> list[tuple[int, float, str]]:
        if not pivots:
            return []

        cleaned = [pivots[0]]
        for pivot in pivots[1:]:
            last = cleaned[-1]
            if pivot[2] != last[2]:
                cleaned.append(pivot)
                continue

            # Если два одинаковых типа подряд — оставляем более экстремальный
            if pivot[2] == "high" and pivot[1] > last[1]:
                cleaned[-1] = pivot
            elif pivot[2] == "low" and pivot[1] < last[1]:
                cleaned[-1] = pivot

        return cleaned

    def _build_wave_candidate(self, seq: list[tuple[int, float, str]]) -> Optional[dict]:
        if len(seq) != 6:
            return None

        idx = [p[0] for p in seq]
        price = [p[1] for p in seq]
        ptype = [p[2] for p in seq]

        direction = None
        if ptype == ["low", "high", "low", "high", "low", "high"]:
            direction = "long"
        elif ptype == ["high", "low", "high", "low", "high", "low"]:
            direction = "short"
        else:
            return None

        leg1 = abs(price[1] - price[0])
        leg2 = abs(price[2] - price[1])
        leg3 = abs(price[3] - price[2])
        leg4 = abs(price[4] - price[3])
        leg5 = abs(price[5] - price[4])

        if min(leg1, leg2, leg3, leg4, leg5) <= 0:
            return None

        if direction == "long":
            structural_ok = price[2] > price[0] and price[4] > price[2] and price[5] > price[3]
        else:
            structural_ok = price[2] < price[0] and price[4] < price[2] and price[5] < price[3]

        if not structural_ok:
            return None

        score = 50.0

        # Волна 3 не должна быть самой короткой
        if leg3 > min(leg1, leg5):
            score += 12.0
        else:
            score -= 10.0

        # Коррекции 2 и 4 умеренные
        corr2 = leg2 / leg1 if leg1 else 0
        corr4 = leg4 / leg3 if leg3 else 0

        if 0.2 <= corr2 <= 0.9:
            score += 8.0
        else:
            score -= 5.0

        if 0.15 <= corr4 <= 0.8:
            score += 8.0
        else:
            score -= 5.0

        # Волна 5 не должна быть микроскопической
        if leg5 >= leg1 * 0.5:
            score += 7.0
        else:
            score -= 6.0

        total_move_pct = abs((price[5] - price[0]) / price[0]) * 100.0 if price[0] else 0.0
        score += min(total_move_pct * 1.5, 15.0)

        return {
            "direction": direction,
            "score": round(max(0.0, min(score, 100.0)), 2),
            "wave1_start_price": float(price[0]),
            "wave1_end_price": float(price[1]),
            "wave2_end_price": float(price[2]),
            "wave3_end_price": float(price[3]),
            "wave4_end_price": float(price[4]),
            "wave5_end_price": float(price[5]),
            "wave1_length": float(leg1),
            "wave3_length": float(leg3),
            "wave5_length": float(leg5),
            "wave4_depth_pct": float(corr4 * 100.0),
            "indexes": idx,
        }