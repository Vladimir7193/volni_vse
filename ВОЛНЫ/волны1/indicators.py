"""
Технические индикаторы: ZigZag, ATR, объёмы
"""

import numpy as np
import pandas as pd
from loguru import logger
from config import ZIGZAG_SETTINGS


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder EMA)"""
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return atr


def zigzag(
    df: pd.DataFrame,
    deviation: float = 5.0,
    depth: int = 10,
    backstep: int = 3
) -> list[dict]:
    highs = df["high"].values
    lows = df["low"].values

    n = len(highs)
    if n < depth:
        return []

    deviation_pct = deviation / 100.0
    pivots = []
    last_pivot_type = None
    last_pivot_price = None
    last_pivot_idx = None

    init_high_idx = np.argmax(highs[:depth])
    init_low_idx = np.argmin(lows[:depth])

    if init_high_idx < init_low_idx:
        last_pivot_type = "high"
        last_pivot_price = highs[init_high_idx]
        last_pivot_idx = init_high_idx
        pivots.append({
            "index": int(init_high_idx),
            "price": float(last_pivot_price),
            "type": "high",
            "bar_index": int(init_high_idx)
        })
    else:
        last_pivot_type = "low"
        last_pivot_price = lows[init_low_idx]
        last_pivot_idx = init_low_idx
        pivots.append({
            "index": int(init_low_idx),
            "price": float(last_pivot_price),
            "type": "low",
            "bar_index": int(init_low_idx)
        })

    i = max(init_high_idx, init_low_idx) + 1
    max_iterations = n * 3
    iteration = 0

    while i < n:
        iteration += 1
        if iteration > max_iterations:
            logger.warning("ZigZag: превышен лимит итераций, выход")
            break

        if last_pivot_type == "high":
            window_end = min(i + depth, n)
            window_low_idx = i + np.argmin(lows[i:window_end])
            window_low = lows[window_low_idx]

            if last_pivot_price > 0 and (last_pivot_price - window_low) / last_pivot_price >= deviation_pct:
                if window_low_idx > last_pivot_idx:
                    segment_high = np.max(highs[last_pivot_idx:window_low_idx + 1])
                    if segment_high > last_pivot_price:
                        new_high_idx = last_pivot_idx + np.argmax(
                            highs[last_pivot_idx:window_low_idx + 1]
                        )
                        pivots[-1] = {
                            "index": int(new_high_idx),
                            "price": float(segment_high),
                            "type": "high",
                            "bar_index": int(new_high_idx)
                        }
                        last_pivot_price = segment_high
                        last_pivot_idx = new_high_idx

                pivots.append({
                    "index": int(window_low_idx),
                    "price": float(window_low),
                    "type": "low",
                    "bar_index": int(window_low_idx)
                })
                last_pivot_type = "low"
                last_pivot_price = window_low
                last_pivot_idx = window_low_idx
                i = window_low_idx + backstep
            else:
                window_high = np.max(highs[i:window_end])
                if window_high > last_pivot_price:
                    new_high_idx = i + np.argmax(highs[i:window_end])
                    pivots[-1] = {
                        "index": int(new_high_idx),
                        "price": float(window_high),
                        "type": "high",
                        "bar_index": int(new_high_idx)
                    }
                    last_pivot_price = window_high
                    last_pivot_idx = new_high_idx
                i += 1

        else:
            window_end = min(i + depth, n)
            window_high_idx = i + np.argmax(highs[i:window_end])
            window_high = highs[window_high_idx]

            if last_pivot_price > 0 and (window_high - last_pivot_price) / last_pivot_price >= deviation_pct:
                if window_high_idx > last_pivot_idx:
                    segment_low = np.min(lows[last_pivot_idx:window_high_idx + 1])
                    if segment_low < last_pivot_price:
                        new_low_idx = last_pivot_idx + np.argmin(
                            lows[last_pivot_idx:window_high_idx + 1]
                        )
                        pivots[-1] = {
                            "index": int(new_low_idx),
                            "price": float(segment_low),
                            "type": "low",
                            "bar_index": int(new_low_idx)
                        }
                        last_pivot_price = segment_low
                        last_pivot_idx = new_low_idx

                pivots.append({
                    "index": int(window_high_idx),
                    "price": float(window_high),
                    "type": "high",
                    "bar_index": int(window_high_idx)
                })
                last_pivot_type = "high"
                last_pivot_price = window_high
                last_pivot_idx = window_high_idx
                i = window_high_idx + backstep
            else:
                window_low = np.min(lows[i:window_end])
                if window_low < last_pivot_price:
                    new_low_idx = i + np.argmin(lows[i:window_end])
                    pivots[-1] = {
                        "index": int(new_low_idx),
                        "price": float(window_low),
                        "type": "low",
                        "bar_index": int(new_low_idx)
                    }
                    last_pivot_price = window_low
                    last_pivot_idx = new_low_idx
                i += 1

    return pivots


def find_zigzag_legs(pivots: list[dict]) -> list[dict]:
    legs = []
    for i in range(len(pivots) - 1):
        start = pivots[i]
        end = pivots[i + 1]

        direction = "up" if end["price"] > start["price"] else "down"
        size = abs(end["price"] - start["price"])
        size_pct = (size / start["price"]) * 100 if start["price"] > 0 else 0

        legs.append({
            "start": start,
            "end": end,
            "direction": direction,
            "size": size,
            "size_pct": size_pct,
            "bars": end["bar_index"] - start["bar_index"]
        })

    return legs


def get_zigzag_for_timeframe(df: pd.DataFrame, timeframe: str) -> tuple[list[dict], list[dict]]:
    settings = ZIGZAG_SETTINGS.get(timeframe, ZIGZAG_SETTINGS["15"])
    pivots = zigzag(df, **settings)
    legs = find_zigzag_legs(pivots)
    return pivots, legs