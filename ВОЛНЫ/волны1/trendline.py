"""
trendline.py — pivot-based trendline and flag pattern utilities.
"""

from __future__ import annotations

from typing import Optional, Any
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


class TrendlineAnalyzer:
    def __init__(self, pivot_window: int = 3) -> None:
        self.pivot_window = pivot_window

    def find_flag_pattern(self, df: pd.DataFrame) -> Optional[dict]:
        if df is None or len(df) < 40:
            return None

        try:
            highs = self._find_pivot_highs(df)
            lows = self._find_pivot_lows(df)

            if len(highs) < 2 or len(lows) < 2:
                return None

            recent_highs = highs[-2:]
            recent_lows = lows[-2:]

            h1_idx, h1_price = recent_highs[0]
            h2_idx, h2_price = recent_highs[1]
            l1_idx, l1_price = recent_lows[0]
            l2_idx, l2_price = recent_lows[1]

            upper_slope = self._slope(h1_idx, h1_price, h2_idx, h2_price)
            lower_slope = self._slope(l1_idx, l1_price, l2_idx, l2_price)

            if math.isnan(upper_slope) or math.isnan(lower_slope):
                return None

            current_idx = len(df) - 1
            upper_now = self._line_value(h1_idx, h1_price, upper_slope, current_idx)
            lower_now = self._line_value(l1_idx, l1_price, lower_slope, current_idx)
            current_close = _safe_float(df["close"].iloc[-1])

            direction = None
            breakout_level = 0.0
            stop_loss = 0.0

            # Bull flag: обе линии слегка вниз/флэт, цена выше верхней границы
            if upper_slope <= 0 and lower_slope <= 0 and current_close > upper_now:
                direction = "long"
                breakout_level = upper_now
                stop_loss = min(l1_price, l2_price)

            # Bear flag: обе линии слегка вверх/флэт, цена ниже нижней границы
            elif upper_slope >= 0 and lower_slope >= 0 and current_close < lower_now:
                direction = "short"
                breakout_level = lower_now
                stop_loss = max(h1_price, h2_price)

            if not direction or breakout_level <= 0 or stop_loss <= 0:
                return None

            impulse_size_pct = self._impulse_size_pct(df)
            flag_depth_pct = self._flag_depth_pct(df)

            return {
                "direction": direction,
                "breakout_level": float(breakout_level),
                "stop_loss": float(stop_loss),
                "impulse_size_pct": float(impulse_size_pct),
                "flag_depth_pct": float(flag_depth_pct),
                "upper_slope": float(upper_slope),
                "lower_slope": float(lower_slope),
            }

        except Exception as exc:
            logger.debug("find_flag_pattern failed: {}", exc)
            return None

    def build_breakout_line(self, df: pd.DataFrame, direction: str) -> Optional[dict]:
        if df is None or len(df) < 25:
            return None

        try:
            current_idx = len(df) - 1
            current_close = _safe_float(df["close"].iloc[-1])

            if direction == "long":
                pivots = self._find_pivot_highs(df)
            else:
                pivots = self._find_pivot_lows(df)

            if len(pivots) < 2:
                return None

            p1_idx, p1_price = pivots[-2]
            p2_idx, p2_price = pivots[-1]

            slope = self._slope(p1_idx, p1_price, p2_idx, p2_price)
            breakout_level = self._line_value(p1_idx, p1_price, slope, current_idx)

            if breakout_level <= 0:
                return None

            trendline_broken = current_close > breakout_level if direction == "long" else current_close < breakout_level

            return {
                "breakout_level": float(breakout_level),
                "trendline_broken": bool(trendline_broken),
                "slope": float(slope),
                "anchor_1_index": int(p1_idx),
                "anchor_1_price": float(p1_price),
                "anchor_2_index": int(p2_idx),
                "anchor_2_price": float(p2_price),
            }

        except Exception as exc:
            logger.debug("build_breakout_line failed: {}", exc)
            return None

    def _find_pivot_highs(self, df: pd.DataFrame) -> list[tuple[int, float]]:
        highs = df["high"].tolist()
        w = self.pivot_window
        pivots: list[tuple[int, float]] = []

        for i in range(w, len(highs) - w):
            value = highs[i]
            left = highs[i - w:i]
            right = highs[i + 1:i + 1 + w]
            if value >= max(left) and value >= max(right):
                pivots.append((i, float(value)))

        return pivots

    def _find_pivot_lows(self, df: pd.DataFrame) -> list[tuple[int, float]]:
        lows = df["low"].tolist()
        w = self.pivot_window
        pivots: list[tuple[int, float]] = []

        for i in range(w, len(lows) - w):
            value = lows[i]
            left = lows[i - w:i]
            right = lows[i + 1:i + 1 + w]
            if value <= min(left) and value <= min(right):
                pivots.append((i, float(value)))

        return pivots

    def _slope(self, x1: int, y1: float, x2: int, y2: float) -> float:
        if x2 == x1:
            return float("nan")
        return (y2 - y1) / (x2 - x1)

    def _line_value(self, x1: int, y1: float, slope: float, x: int) -> float:
        return y1 + slope * (x - x1)

    def _impulse_size_pct(self, df: pd.DataFrame, lookback: int = 20) -> float:
        window = df.iloc[-lookback:]
        low = _safe_float(window["low"].min())
        high = _safe_float(window["high"].max())
        if low <= 0 or high <= 0:
            return 0.0
        return abs((high - low) / low) * 100.0

    def _flag_depth_pct(self, df: pd.DataFrame, lookback: int = 10) -> float:
        window = df.iloc[-lookback:]
        low = _safe_float(window["low"].min())
        high = _safe_float(window["high"].max())
        if low <= 0 or high <= 0:
            return 0.0
        return abs((high - low) / low) * 100.0