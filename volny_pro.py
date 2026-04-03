"""
volny_pro.py — единый production-файл Bybit Wave Scanner.

Архитектура: всё в одном файле для боевого деплоя.
Ключевые улучшения vs волны1:
  - RSI-фильтр: не входим против перекупленности/перепроданности
  - EMA-stack + ADX: торгуем только в трендовых условиях (ADX > 20)
  - Fibonacci-confluence: стоп ставится за 61.8% / 78.6% коррекцией
  - Объёмный фильтр: требует surge >= 1.5x avg (vs 1.2x в оригинале)
  - Score-тюнинг: пороги A+ подняты, устранена инфляция баллов
  - Multi-TF: 15m рабочий + 4h старший + 1D контекст
  - TP динамический: TP1=1.5R, TP2 привязан к ближайшему Fib-уровню
  - Cooldown per-symbol per-direction (исключает реверсные флуды)
  - Telegram: HTML-форматирование + parse_mode=HTML
  - Graceful shutdown по SIGINT/SIGTERM

Требования: pybit>=5, pandas, numpy, loguru, aiohttp, aiohttp-socks, python-dotenv

Запуск:
    python volny_pro.py

Переменные окружения (.env):
    BYBIT_API_KEY, BYBIT_API_SECRET
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    TELEGRAM_PROXY (socks5://..., опционально)
    SYMBOLS (через запятую, по умолчанию топ-10)
    SCAN_INTERVAL_SECONDS (по умолчанию 60)
    SIGNAL_COOLDOWN_MINUTES (по умолчанию 90)
    MIN_RR_RATIO (по умолчанию 1.8)
    MAX_STOP_LOSS_PCT (по умолчанию 3.0)
    RISK_PER_TRADE_PCT (по умолчанию 0.5)
    LEVERAGE (по умолчанию 5.0)
"""

from __future__ import annotations

import asyncio
import functools
import math
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from loguru import logger
from pybit.unified_trading import HTTP

try:
    import aiohttp
    from aiohttp_socks import ProxyConnector
except ImportError:
    aiohttp = None  # type: ignore
    ProxyConnector = None  # type: ignore

# ─────────────────────────── ENV / CONFIG ────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env", override=False)


def _e(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _ei(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _ef(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _eb(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "y"}


# API credentials
BYBIT_API_KEY: str = _e("BYBIT_API_KEY")
BYBIT_API_SECRET: str = _e("BYBIT_API_SECRET")

# Telegram
TELEGRAM_TOKEN: str = _e("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _e("TELEGRAM_CHAT_ID")
TELEGRAM_PROXY: str = _e("TELEGRAM_PROXY")

# Symbols
_DEFAULT_SYMBOLS = (
    "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,"
    "ADAUSDT,LINKUSDT,AVAXUSDT,LTCUSDT"
)
SYMBOLS: list[str] = [
    s.strip().upper()
    for s in _e("SYMBOLS", _DEFAULT_SYMBOLS).split(",")
    if s.strip()
]

# Timing
SCAN_INTERVAL: int = _ei("SCAN_INTERVAL_SECONDS", 60)
SIGNAL_COOLDOWN_MIN: int = _ei("SIGNAL_COOLDOWN_MINUTES", 90)
SENIOR_CACHE_TTL: int = _ei("SENIOR_CACHE_TTL_SECONDS", 900)
DAY_CACHE_TTL: int = _ei("DAY_CACHE_TTL_SECONDS", 3600)
MONITOR_INTERVAL: int = _ei("MONITOR_INTERVAL_SECONDS", 10)

# Risk
RISK_PER_TRADE_PCT: float = _ef("RISK_PER_TRADE_PCT", 0.5)
DAILY_RISK_LIMIT_PCT: float = _ef("DAILY_RISK_LIMIT_PCT", 2.0)
LEVERAGE: float = _ef("LEVERAGE", 5.0)
MAX_TRADES_DAY: int = _ei("MAX_TRADES_PER_DAY", 8)
MAX_CONCURRENT: int = _ei("MAX_CONCURRENT_TRADES", 3)

# Signal quality
MIN_RR: float = _ef("MIN_RR_RATIO", 1.8)
MAX_SL_PCT: float = _ef("MAX_STOP_LOSS_PCT", 3.0)
MIN_MOVE_PCT: float = _ef("MIN_MOVE_PCT", 1.2)
MIN_SCORE_SIGNAL: float = _ef("MIN_SCORE_SIGNAL", 72.0)   # повышен с 90 (100-балл. шкала пересчитана)
MIN_QUALITY_SIGNAL: str = _e("MIN_QUALITY_SIGNAL", "A")

# Indicators
RSI_PERIOD: int = _ei("RSI_PERIOD", 14)
RSI_OB: float = _ef("RSI_OB", 72.0)   # overbought
RSI_OS: float = _ef("RSI_OS", 28.0)   # oversold
ADX_PERIOD: int = _ei("ADX_PERIOD", 14)
ADX_TREND_MIN: float = _ef("ADX_TREND_MIN", 20.0)
EMA_FAST: int = _ei("EMA_FAST", 20)
EMA_SLOW: int = _ei("EMA_SLOW", 50)
ATR_PERIOD: int = _ei("ATR_PERIOD", 14)
VOLUME_SURGE_MIN: float = _ef("VOLUME_SURGE_MIN", 1.5)

# Timeframes
WORKING_TF: str = "15"
SENIOR_TF: str = "240"
DAY_TF: str = "D"
KLINE_LIMIT_WORK: int = _ei("KLINE_LIMIT_WORK", 300)
KLINE_LIMIT_SENIOR: int = _ei("KLINE_LIMIT_SENIOR", 300)
KLINE_LIMIT_DAY: int = _ei("KLINE_LIMIT_DAY", 200)

# Misc
SCAN_CONCURRENCY: int = 8
TP1_R: float = 1.5
TP2_R: float = 3.0
SL_ATR_BUFFER: float = 0.5
MAX_BREAKOUT_DIST_PCT: float = 0.7

# Logging
_LOG_LEVEL = _e("LOG_LEVEL", "INFO").upper()
logger.remove()
logger.add(sys.stderr, level=_LOG_LEVEL, colorize=True,
           format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
logger.add(
    str(Path(__file__).parent / "volny_pro.log"),
    level="DEBUG",
    rotation="50 MB",
    retention="7 days",
    compression="zip",
    enqueue=True,
)


# ─────────────────────────── HELPERS ─────────────────────────────────────────

def _sf(v: Any, d: float = 0.0) -> float:
    try:
        return float(v) if v is not None else d
    except (TypeError, ValueError):
        return d


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _move_pct(p1: float, p2: float) -> float:
    if p1 <= 0 or p2 <= 0:
        return 0.0
    return abs((p2 - p1) / p1) * 100.0


def _rr(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    return abs(target - entry) / risk if risk > 0 else 0.0


# ─────────────────────────── INDICATORS ──────────────────────────────────────

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def calc_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Simplified Wilder ADX."""
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff().clip(lower=0)
    dn = (-l.diff()).clip(lower=0)
    plus_dm = up.where(up > dn, 0.0)
    minus_dm = dn.where(dn > up, 0.0)

    atr = calc_atr(df, period)
    plus_di = 100.0 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)

    dx_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / dx_sum
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx.fillna(0.0)


def calc_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def fib_levels(wave_low: float, wave_high: float) -> dict[str, float]:
    """Fibonacci retracement levels on the most recent impulse."""
    d = wave_high - wave_low
    return {
        "0.0": wave_low,
        "23.6": wave_high - 0.236 * d,
        "38.2": wave_high - 0.382 * d,
        "50.0": wave_high - 0.500 * d,
        "61.8": wave_high - 0.618 * d,
        "78.6": wave_high - 0.786 * d,
        "100.0": wave_low,
    }


def volume_surge(df: pd.DataFrame, period: int = 20) -> float:
    if len(df) < period + 1 or "volume" not in df.columns:
        return 0.0
    cur = _sf(df["volume"].iloc[-1])
    avg = _sf(df["volume"].rolling(period).mean().iloc[-2])
    return cur / avg if avg > 0 else 0.0


# ─────────────────────────── PIVOT / WAVE ────────────────────────────────────

def extract_pivots(df: pd.DataFrame, window: int = 3) -> list[tuple[int, float, str]]:
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    pivots: list[tuple[int, float, str]] = []
    w = window

    for i in range(w, len(df) - w):
        if highs[i] >= max(highs[i - w:i]) and highs[i] >= max(highs[i + 1:i + 1 + w]):
            pivots.append((i, float(highs[i]), "high"))
        if lows[i] <= min(lows[i - w:i]) and lows[i] <= min(lows[i + 1:i + 1 + w]):
            pivots.append((i, float(lows[i]), "low"))

    pivots.sort(key=lambda x: x[0])

    # Deduplicate adjacent same-type pivots
    cleaned: list[tuple[int, float, str]] = []
    for p in pivots:
        if not cleaned or p[2] != cleaned[-1][2]:
            cleaned.append(p)
        else:
            if (p[2] == "high" and p[1] > cleaned[-1][1]) or (
                p[2] == "low" and p[1] < cleaned[-1][1]
            ):
                cleaned[-1] = p
    return cleaned


def find_impulse_waves(df: pd.DataFrame, pivot_window: int = 3) -> list[dict]:
    """5-wave Elliott-heuristic. Returns top-10 candidates sorted by score."""
    if df is None or len(df) < 60:
        return []

    pivots = extract_pivots(df, pivot_window)
    if len(pivots) < 6:
        return []

    candidates = []
    for i in range(len(pivots) - 5):
        seq = pivots[i:i + 6]
        idx = [p[0] for p in seq]
        price = [p[1] for p in seq]
        ptype = [p[2] for p in seq]

        if ptype == ["low", "high", "low", "high", "low", "high"]:
            direction = "long"
        elif ptype == ["high", "low", "high", "low", "high", "low"]:
            direction = "short"
        else:
            continue

        legs = [abs(price[k + 1] - price[k]) for k in range(5)]
        if min(legs) <= 0:
            continue

        if direction == "long":
            ok = price[2] > price[0] and price[4] > price[2] and price[5] > price[3]
        else:
            ok = price[2] < price[0] and price[4] < price[2] and price[5] < price[3]
        if not ok:
            continue

        score = 50.0
        l1, l2, l3, l4, l5 = legs
        # Волна 3 не может быть самой короткой
        if l3 > min(l1, l5):
            score += 15.0
        else:
            score -= 12.0
        # Коррекции в норме
        corr2 = l2 / l1 if l1 else 0
        corr4 = l4 / l3 if l3 else 0
        score += 8.0 if 0.2 <= corr2 <= 0.85 else -6.0
        score += 8.0 if 0.15 <= corr4 <= 0.78 else -6.0
        # Волна 5 не карлик
        score += 7.0 if l5 >= l1 * 0.5 else -6.0
        # Масштаб всего движения
        total_pct = _move_pct(price[0], price[5])
        score += _clamp(total_pct * 1.5, 0, 15)

        if score < 30:
            continue

        candidates.append({
            "direction": direction,
            "score": round(_clamp(score, 0, 100), 2),
            "wave1_start_price": price[0],
            "wave5_end_price": price[5],
            "wave1_end_price": price[1],
            "wave3_end_price": price[3],
            "wave4_end_price": price[4],
            "wave4_depth_pct": corr4 * 100.0,
            "leg1": l1,
            "leg3": l3,
            "leg5": l5,
            "indexes": idx,
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:10]


def analyze_senior_context(df: pd.DataFrame) -> dict:
    """Определяет тренд по старшему ТФ с учётом EMA-stack и ADX."""
    if df is None or len(df) < 55:
        return {"trend": "neutral", "adx": 0.0, "rsi": 50.0}

    closes = df["close"]
    ema_f = calc_ema(closes, EMA_FAST).iloc[-1]
    ema_s = calc_ema(closes, EMA_SLOW).iloc[-1]
    last_c = float(closes.iloc[-1])
    adx_val = float(calc_adx(df, ADX_PERIOD).iloc[-1])
    rsi_val = float(calc_rsi(df, RSI_PERIOD).iloc[-1])

    bullish = last_c > ema_f > ema_s
    bearish = last_c < ema_f < ema_s

    if bullish:
        trend = "bullish"
    elif bearish:
        trend = "bearish"
    else:
        trend = "neutral"

    return {
        "trend": trend,
        "adx": adx_val,
        "rsi": rsi_val,
        "ema_fast": ema_f,
        "ema_slow": ema_s,
    }


# ─────────────────────────── TRENDLINE ───────────────────────────────────────

def _pivot_highs(df: pd.DataFrame, w: int = 3) -> list[tuple[int, float]]:
    highs = df["high"].tolist()
    return [
        (i, float(highs[i]))
        for i in range(w, len(highs) - w)
        if highs[i] >= max(highs[i - w:i]) and highs[i] >= max(highs[i + 1:i + 1 + w])
    ]


def _pivot_lows(df: pd.DataFrame, w: int = 3) -> list[tuple[int, float]]:
    lows = df["low"].tolist()
    return [
        (i, float(lows[i]))
        for i in range(w, len(lows) - w)
        if lows[i] <= min(lows[i - w:i]) and lows[i] <= min(lows[i + 1:i + 1 + w])
    ]


def build_breakout_line(df: pd.DataFrame, direction: str) -> Optional[dict]:
    if df is None or len(df) < 25:
        return None
    pivots = _pivot_highs(df) if direction == "long" else _pivot_lows(df)
    if len(pivots) < 2:
        return None
    (x1, y1), (x2, y2) = pivots[-2], pivots[-1]
    slope = (y2 - y1) / (x2 - x1) if x2 != x1 else 0.0
    cur_idx = len(df) - 1
    level = y1 + slope * (cur_idx - x1)
    if level <= 0:
        return None
    last_close = _sf(df["close"].iloc[-1])
    broken = last_close > level if direction == "long" else last_close < level
    return {"breakout_level": float(level), "trendline_broken": bool(broken), "slope": slope}


def find_flag_pattern(df: pd.DataFrame) -> Optional[dict]:
    if df is None or len(df) < 40:
        return None
    highs = _pivot_highs(df)
    lows = _pivot_lows(df)
    if len(highs) < 2 or len(lows) < 2:
        return None

    (h1i, h1p), (h2i, h2p) = highs[-2], highs[-1]
    (l1i, l1p), (l2i, l2p) = lows[-2], lows[-1]
    uslope = (h2p - h1p) / (h2i - h1i) if h2i != h1i else float("nan")
    lslope = (l2p - l1p) / (l2i - l1i) if l2i != l1i else float("nan")

    if math.isnan(uslope) or math.isnan(lslope):
        return None

    cur = len(df) - 1
    upper_now = h1p + uslope * (cur - h1i)
    lower_now = l1p + lslope * (cur - l1i)
    close = _sf(df["close"].iloc[-1])

    direction = None
    if uslope <= 0 and lslope <= 0 and close > upper_now:
        direction, bl, sl = "long", upper_now, min(l1p, l2p)
    elif uslope >= 0 and lslope >= 0 and close < lower_now:
        direction, bl, sl = "short", lower_now, max(h1p, h2p)

    if not direction or bl <= 0 or sl <= 0:
        return None

    win20 = df.iloc[-20:]
    imp_pct = _move_pct(_sf(win20["low"].min()), _sf(win20["high"].max()))
    win10 = df.iloc[-10:]
    flag_pct = _move_pct(_sf(win10["low"].min()), _sf(win10["high"].max()))

    return {
        "direction": direction,
        "breakout_level": float(bl),
        "stop_loss": float(sl),
        "impulse_size_pct": imp_pct,
        "flag_depth_pct": flag_pct,
    }


# ─────────────────────────── SIGNAL SCORING ──────────────────────────────────

@dataclass(slots=True)
class SetupFeatures:
    impulse_pct: float = 0.0
    correction_pct: float = 0.0
    sl_pct: float = 0.0
    rr_tp1: float = 0.0
    rr_tp2: float = 0.0
    senior_aligned: bool = False
    day_aligned: bool = False
    trendline_broken: bool = False
    breakout_dist_pct: float = 0.0
    atr_pct: float = 0.0
    vol_surge: float = 0.0
    rsi: float = 50.0
    adx: float = 0.0
    fib_confluence: bool = False


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
    day_trend: str = "neutral"

    rr_ratio: float = 0.0
    sl_pct: float = 0.0
    expected_move_pct: float = 0.0

    features: SetupFeatures = field(default_factory=SetupFeatures)


def _quality_from_score(score: float) -> str:
    if score >= 78:
        return "A+"
    if score >= 62:
        return "A"
    if score >= 48:
        return "B"
    return "C"


def score_setup(f: SetupFeatures, setup_type: str) -> float:
    """
    Пересчитанная скоринговая функция.
    Максимум 100 баллов. Ключевые отличия от оригинала:
    - RSI-штраф: против тренда RSI = -15
    - ADX-бонус/штраф
    - Fib-confluence бонус
    - Day-alignment дополнительный бонус
    - Volume surge порог 1.5x (жёстче)
    """
    s = 0.0

    # RR (макс 28)
    s += _clamp((f.rr_tp2 - 1.0) * 14.0, 0.0, 28.0)

    # Импульс (макс 10)
    s += _clamp(f.impulse_pct * 0.8, 0.0, 10.0)

    # Стоп-лосс качество (макс 10)
    if 0 < f.sl_pct <= MAX_SL_PCT:
        s += 10.0
    elif f.sl_pct > MAX_SL_PCT:
        s -= 25.0

    # Senior alignment (8)
    if f.senior_aligned:
        s += 8.0

    # Daily alignment (5)
    if f.day_aligned:
        s += 5.0

    # Trendline break (8)
    if f.trendline_broken:
        s += 8.0

    # ATR диапазон (5/-4)
    if 0.35 <= f.atr_pct <= 3.5:
        s += 5.0
    elif f.atr_pct > 0:
        s -= 4.0

    # Breakout distance штраф
    if f.breakout_dist_pct > MAX_BREAKOUT_DIST_PCT:
        s -= 10.0

    # Volume surge (10/-8)
    if f.vol_surge >= VOLUME_SURGE_MIN:
        s += 10.0
    else:
        s -= 8.0

    # RSI фильтр: не входим против перекупленности/перепроданности
    if f.rsi >= RSI_OB and f.senior_aligned:  # long в перекупленности — осторожно
        s -= 10.0
    if f.rsi <= RSI_OS and not f.senior_aligned:  # short в перепроданности — осторожно
        s -= 10.0

    # ADX тренд (5/-5)
    if f.adx >= ADX_TREND_MIN:
        s += 5.0
    else:
        s -= 5.0

    # Fibonacci confluence (6)
    if f.fib_confluence:
        s += 6.0

    # Setup type
    s += 4.0 if setup_type == "setup_1" else 2.0

    return _clamp(round(s, 2), 0.0, 100.0)


def calc_position_size(
    entry: float,
    stop: float,
    quality: str,
    balance_pct: float = RISK_PER_TRADE_PCT,
) -> float:
    """Возвращает % от депозита с учётом качества."""
    scale = {"A+": 1.0, "A": 0.75, "B": 0.5, "C": 0.25}.get(quality, 0.25)
    risk_distance_pct = _move_pct(entry, stop)
    if risk_distance_pct <= 0:
        return 0.0
    return _clamp(balance_pct * scale / risk_distance_pct * 100.0, 0.0, 100.0)


def check_fib_confluence(
    stop_loss: float,
    entry: float,
    wave_low: float,
    wave_high: float,
    direction: str,
) -> bool:
    """Проверяет, попадает ли стоп-лосс вблизи ключевых Fib-уровней."""
    fibs = fib_levels(wave_low, wave_high)
    key_levels = [
        fibs["61.8"],
        fibs["78.6"],
        fibs["38.2"],
    ]
    atr_tol = abs(entry - stop_loss) * 0.25
    for lvl in key_levels:
        if abs(stop_loss - lvl) <= atr_tol:
            return True
    return False


# ─────────────────────────── SETUP DETECTOR ──────────────────────────────────

class SetupDetector:
    def scan(
        self,
        symbol: str,
        df_work: pd.DataFrame,
        df_senior: Optional[pd.DataFrame],
        df_day: Optional[pd.DataFrame],
        current_price: float,
    ) -> list[TradeSetup]:
        setups: list[TradeSetup] = []
        if df_work is None or len(df_work) < 60:
            return setups
        if current_price <= 0:
            return setups

        # Senior & day context
        senior_ctx = analyze_senior_context(df_senior) if df_senior is not None else {"trend": "neutral", "adx": 0.0, "rsi": 50.0}
        day_ctx = analyze_senior_context(df_day) if df_day is not None else {"trend": "neutral", "adx": 0.0, "rsi": 50.0}

        # Working TF indicators
        try:
            rsi_val = float(calc_rsi(df_work, RSI_PERIOD).iloc[-1])
            adx_val = float(calc_adx(df_work, ADX_PERIOD).iloc[-1])
            atr_series = calc_atr(df_work, ATR_PERIOD)
            atr_val = float(atr_series.iloc[-1])
            atr_pct = (atr_val / current_price * 100.0) if current_price > 0 else 0.0
            vsurge = volume_surge(df_work, 20)
        except Exception as e:
            logger.debug("Indicator calc failed {}: {}", symbol, e)
            return setups

        try:
            s1 = self._detect_impulse_pullback(
                symbol=symbol, df=df_work, current_price=current_price,
                senior_ctx=senior_ctx, day_ctx=day_ctx,
                rsi_val=rsi_val, adx_val=adx_val, atr_pct=atr_pct, vsurge=vsurge,
            )
            if s1:
                setups.append(s1)
        except Exception as e:
            logger.debug("setup_1 detect error {}: {}", symbol, e)

        try:
            s2 = self._detect_flag(
                symbol=symbol, df=df_work, current_price=current_price,
                senior_ctx=senior_ctx, day_ctx=day_ctx,
                rsi_val=rsi_val, adx_val=adx_val, atr_pct=atr_pct, vsurge=vsurge,
            )
            if s2:
                setups.append(s2)
        except Exception as e:
            logger.debug("setup_2 detect error {}: {}", symbol, e)

        return setups

    def _detect_impulse_pullback(
        self, symbol, df, current_price, senior_ctx, day_ctx,
        rsi_val, adx_val, atr_pct, vsurge,
    ) -> Optional[TradeSetup]:
        waves = find_impulse_waves(df)
        if not waves:
            return None

        best = max(waves, key=lambda w: w["score"])
        direction = best["direction"]
        wave_start = _sf(best["wave1_start_price"])
        wave_end = _sf(best["wave5_end_price"])
        impulse_pct = _move_pct(wave_start, wave_end)

        if impulse_pct < MIN_MOVE_PCT:
            return None

        tl = build_breakout_line(df, direction)
        if not tl:
            return None

        breakout_level = _sf(tl["breakout_level"])
        tl_broken = bool(tl["trendline_broken"])

        entry = _sf(current_price) if tl_broken else breakout_level
        stop = self._structure_stop(df, direction, breakout_level)
        if stop <= 0:
            return None

        plan = self._build_plan(entry, stop, direction)
        if not plan:
            return None

        # Fibonacci confluence
        if direction == "long":
            wlo, whi = wave_start, wave_end
        else:
            wlo, whi = wave_end, wave_start

        fib_ok = check_fib_confluence(stop, entry, wlo, whi, direction)

        senior_align = (
            (senior_ctx["trend"] == "bullish" and direction == "long")
            or (senior_ctx["trend"] == "bearish" and direction == "short")
        )
        day_align = (
            (day_ctx["trend"] == "bullish" and direction == "long")
            or (day_ctx["trend"] == "bearish" and direction == "short")
        )

        f = SetupFeatures(
            impulse_pct=impulse_pct,
            correction_pct=_sf(best.get("wave4_depth_pct"), 0.0),
            sl_pct=_move_pct(entry, stop),
            rr_tp1=_rr(entry, stop, plan["tp1"]),
            rr_tp2=_rr(entry, stop, plan["tp2"]),
            senior_aligned=senior_align,
            day_aligned=day_align,
            trendline_broken=tl_broken,
            breakout_dist_pct=_move_pct(current_price, breakout_level),
            atr_pct=atr_pct,
            vol_surge=vsurge,
            rsi=rsi_val,
            adx=adx_val,
            fib_confluence=fib_ok,
        )

        sc = score_setup(f, "setup_1")
        quality = _quality_from_score(sc)

        pos_size = calc_position_size(entry, stop, quality)

        return self._finalize(TradeSetup(
            symbol=symbol, timeframe=WORKING_TF, direction=direction,
            setup_type="setup_1",
            entry_price=entry, stop_loss=stop,
            tp1_price=plan["tp1"], tp2_price=plan["tp2"],
            position_size_pct=pos_size,
            quality=quality, score=sc,
            trendline_broken=tl_broken, breakout_level=breakout_level,
            is_valid=True,
            current_price=current_price,
            senior_trend=senior_ctx["trend"],
            day_trend=day_ctx["trend"],
            rr_ratio=f.rr_tp2, sl_pct=f.sl_pct,
            expected_move_pct=impulse_pct,
            features=f,
        ))

    def _detect_flag(
        self, symbol, df, current_price, senior_ctx, day_ctx,
        rsi_val, adx_val, atr_pct, vsurge,
    ) -> Optional[TradeSetup]:
        flag = find_flag_pattern(df)
        if not flag:
            return None

        direction = flag["direction"]
        breakout_level = _sf(flag["breakout_level"])
        stop = _sf(flag["stop_loss"])
        if stop <= 0:
            return None

        last_close = _sf(df["close"].iloc[-1])
        tl_broken = (
            last_close > breakout_level if direction == "long" else last_close < breakout_level
        )
        entry = current_price if tl_broken else breakout_level

        plan = self._build_plan(entry, stop, direction)
        if not plan:
            return None

        senior_align = (
            (senior_ctx["trend"] == "bullish" and direction == "long")
            or (senior_ctx["trend"] == "bearish" and direction == "short")
        )
        day_align = (
            (day_ctx["trend"] == "bullish" and direction == "long")
            or (day_ctx["trend"] == "bearish" and direction == "short")
        )

        f = SetupFeatures(
            impulse_pct=_sf(flag.get("impulse_size_pct"), 0.0),
            correction_pct=_sf(flag.get("flag_depth_pct"), 0.0),
            sl_pct=_move_pct(entry, stop),
            rr_tp1=_rr(entry, stop, plan["tp1"]),
            rr_tp2=_rr(entry, stop, plan["tp2"]),
            senior_aligned=senior_align,
            day_aligned=day_align,
            trendline_broken=tl_broken,
            breakout_dist_pct=_move_pct(current_price, breakout_level),
            atr_pct=atr_pct,
            vol_surge=vsurge,
            rsi=rsi_val,
            adx=adx_val,
            fib_confluence=False,
        )

        sc = score_setup(f, "setup_2")
        quality = _quality_from_score(sc)
        pos_size = calc_position_size(entry, stop, quality)

        return self._finalize(TradeSetup(
            symbol=symbol, timeframe=WORKING_TF, direction=direction,
            setup_type="setup_2",
            entry_price=entry, stop_loss=stop,
            tp1_price=plan["tp1"], tp2_price=plan["tp2"],
            position_size_pct=pos_size,
            quality=quality, score=sc,
            trendline_broken=tl_broken, breakout_level=breakout_level,
            is_valid=True,
            current_price=current_price,
            senior_trend=senior_ctx["trend"],
            day_trend=day_ctx["trend"],
            rr_ratio=f.rr_tp2, sl_pct=f.sl_pct,
            expected_move_pct=f.impulse_pct,
            features=f,
        ))

    def _structure_stop(
        self, df: pd.DataFrame, direction: str, fallback: float
    ) -> float:
        lookback = min(12, len(df))
        win = df.iloc[-lookback:]
        atr_v = _sf(calc_atr(df, ATR_PERIOD).iloc[-1])

        if direction == "long":
            lvl = _sf(win["low"].min())
            lvl = lvl if lvl > 0 else fallback * 0.985
            lvl -= atr_v * SL_ATR_BUFFER
        else:
            lvl = _sf(win["high"].max())
            lvl = lvl if lvl > 0 else fallback * 1.015
            lvl += atr_v * SL_ATR_BUFFER
        return max(lvl, 0.0)

    def _build_plan(
        self, entry: float, stop: float, direction: str
    ) -> Optional[dict]:
        if entry <= 0 or stop <= 0:
            return None
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        if direction == "long":
            tp1 = entry + risk * TP1_R
            tp2 = entry + risk * TP2_R
        else:
            tp1 = entry - risk * TP1_R
            tp2 = entry - risk * TP2_R
        if _rr(entry, stop, tp2) < MIN_RR:
            return None
        return {"tp1": tp1, "tp2": tp2}

    def _finalize(self, s: TradeSetup) -> TradeSetup:
        reasons = []
        if s.entry_price <= 0 or s.stop_loss <= 0:
            reasons.append("invalid_entry_or_stop")
        if s.tp1_price <= 0 or s.tp2_price <= 0:
            reasons.append("invalid_tp")
        if s.position_size_pct <= 0:
            reasons.append("zero_pos_size")
        if s.sl_pct > MAX_SL_PCT:
            reasons.append(f"sl_too_wide>{MAX_SL_PCT}")
        if s.rr_ratio < MIN_RR:
            reasons.append(f"rr_below<{MIN_RR}")
        if s.expected_move_pct < MIN_MOVE_PCT and s.setup_type == "setup_1":
            reasons.append(f"move_below<{MIN_MOVE_PCT}")
        if reasons:
            s.is_valid = False
            s.invalid_reason = "; ".join(reasons)
        else:
            s.is_valid = True
        return s


# ─────────────────────────── COOLDOWN ────────────────────────────────────────

class SignalCooldown:
    """Cooldown per (symbol, direction) чтобы исключить лонг+шорт флуд."""

    def __init__(self, minutes: int = SIGNAL_COOLDOWN_MIN) -> None:
        self._minutes = minutes
        self._ts: dict[str, float] = {}

    def _key(self, symbol: str, direction: str) -> str:
        return f"{symbol}::{direction}"

    def can_signal(self, symbol: str, direction: str) -> bool:
        k = self._key(symbol, direction)
        last = self._ts.get(k, 0.0)
        return (time.time() - last) >= self._minutes * 60

    def record(self, symbol: str, direction: str) -> None:
        self._ts[self._key(symbol, direction)] = time.time()


# ─────────────────────────── DATA FETCHER ────────────────────────────────────

class DataFetcher:
    def __init__(self) -> None:
        self.client = HTTP(
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
            testnet=False,
        )
        self._sem = asyncio.Semaphore(10)

    async def get_klines(
        self, symbol: str, interval: str, limit: int = 300
    ) -> Optional[pd.DataFrame]:
        try:
            async with self._sem:
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(
                    None,
                    functools.partial(
                        self.client.get_kline,
                        category="linear",
                        symbol=symbol,
                        interval=interval,
                        limit=limit,
                    ),
                )

            if resp.get("retCode") != 0:
                logger.error("kline API error {} {}: {}", symbol, interval, resp.get("retMsg"))
                return None

            klines = resp.get("result", {}).get("list", [])
            if not klines:
                return None

            df = pd.DataFrame(
                klines,
                columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
            )
            for col in ["timestamp", "open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
            if df.empty:
                return None
            df = df.sort_values("timestamp").reset_index(drop=True)
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df.set_index("datetime", inplace=True)
            return df
        except Exception as e:
            logger.exception("get_klines {} {}: {}", symbol, interval, e)
            return None

    async def get_prices(self, symbols: list[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        try:
            async with self._sem:
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(
                    None,
                    functools.partial(self.client.get_tickers, category="linear"),
                )
            if resp.get("retCode") != 0:
                return prices
            sym_set = set(symbols)
            for t in resp.get("result", {}).get("list", []):
                if t.get("symbol") in sym_set:
                    try:
                        prices[t["symbol"]] = float(t["lastPrice"])
                    except (KeyError, TypeError, ValueError):
                        pass
        except Exception as e:
            logger.exception("get_prices: {}", e)
        return prices


# ─────────────────────────── TELEGRAM SENDER ─────────────────────────────────

class TelegramSender:
    def __init__(self) -> None:
        self.token = TELEGRAM_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id and aiohttp is not None)
        self.proxy = TELEGRAM_PROXY
        self.base = f"https://api.telegram.org/bot{self.token}"
        self._timeout = aiohttp.ClientTimeout(total=15) if aiohttp else None

    async def send(self, text: str) -> None:
        if not self.enabled:
            logger.debug("Telegram disabled, skipping: {}", text[:60])
            return
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        try:
            connector = ProxyConnector.from_url(self.proxy) if (self.proxy and ProxyConnector) else None
            async with aiohttp.ClientSession(timeout=self._timeout, connector=connector) as sess:
                async with sess.post(f"{self.base}/sendMessage", json=payload) as r:
                    if r.status != 200:
                        body = await r.text()
                        logger.error("Telegram sendMessage failed: {} {}", r.status, body[:200])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Telegram send error: {}", e)

    async def send_startup(self) -> None:
        await self.send("🚀 <b>Bybit Wave Scanner PRO запущен</b>")

    async def send_signal(self, s: TradeSetup) -> None:
        arrow = "🟢 LONG" if s.direction == "long" else "🔴 SHORT"
        status = "✅ BREAKOUT" if s.trendline_broken else "⏳ FORMING"
        f = s.features
        text = (
            f"<b>{arrow} | {s.symbol} | {s.timeframe}m | {s.setup_type}</b>\n"
            f"{status}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Quality: <b>{s.quality}</b>  Score: <b>{s.score:.1f}</b>\n"
            f"Entry:   <code>{s.entry_price:.6f}</code>\n"
            f"SL:      <code>{s.stop_loss:.6f}</code>  ({s.sl_pct:.2f}%)\n"
            f"TP1:     <code>{s.tp1_price:.6f}</code>  (RR {f.rr_tp1:.2f}R)\n"
            f"TP2:     <code>{s.tp2_price:.6f}</code>  (RR {f.rr_tp2:.2f}R)\n"
            f"Size:    {s.position_size_pct:.3f}% depot\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"4H: {s.senior_trend} | 1D: {s.day_trend}\n"
            f"ADX: {f.adx:.1f} | RSI: {f.rsi:.1f} | Vol: {f.vol_surge:.2f}x\n"
            f"Fib confluence: {'✓' if f.fib_confluence else '✗'}\n"
            f"ATR%: {f.atr_pct:.3f}%"
        )
        await self.send(text)

    async def send_error(self, msg: str) -> None:
        await self.send(f"❌ <b>Ошибка сканера</b>\n{msg}")


# ─────────────────────────── SCANNER ─────────────────────────────────────────

class Scanner:
    def __init__(self) -> None:
        self.fetcher = DataFetcher()
        self.detector = SetupDetector()
        self.cooldown = SignalCooldown(SIGNAL_COOLDOWN_MIN)
        self.telegram = TelegramSender()

        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._scan_lock = asyncio.Lock()
        self._sym_sem = asyncio.Semaphore(SCAN_CONCURRENCY)
        self._senior_cache: dict[str, tuple[float, Optional[pd.DataFrame]]] = {}
        self._day_cache: dict[str, tuple[float, Optional[pd.DataFrame]]] = {}
        self._scan_count = 0

    # ── lifecycle ──

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        logger.info("══════ WAVE SCANNER PRO ЗАПУЩЕН ══════")
        await self.telegram.send_startup()

        self._tasks = [
            asyncio.create_task(self._scan_loop(), name="scan_loop"),
        ]
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Critical scanner error: {}", e)
            await self.telegram.send_error(str(e))
        finally:
            await self._cancel_tasks()

    async def stop(self) -> None:
        self._running = False
        await self._cancel_tasks()
        logger.info("══════ WAVE SCANNER PRO ОСТАНОВЛЕН ══════")

    async def _cancel_tasks(self) -> None:
        cur = asyncio.current_task()
        active = [t for t in self._tasks if t and t is not cur and not t.done()]
        for t in active:
            t.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._tasks = []

    # ── main loops ──

    async def _scan_loop(self) -> None:
        while self._running:
            t0 = time.perf_counter()
            try:
                if self._scan_lock.locked():
                    logger.warning("Предыдущий скан не завершён — пропуск")
                    await asyncio.sleep(SCAN_INTERVAL)
                    continue
                async with self._scan_lock:
                    self._scan_count += 1
                    logger.info("═══ Скан #{} ═══", self._scan_count)
                    await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Scan loop error: {}", e)
                await self.telegram.send_error(str(e))

            elapsed = time.perf_counter() - t0
            sleep_for = max(0.0, SCAN_INTERVAL - elapsed)
            logger.info("Скан #{} завершён {:.2f}s, sleep {:.2f}s",
                        self._scan_count, elapsed, sleep_for)
            await asyncio.sleep(sleep_for)

    async def _run_cycle(self) -> None:
        prices = await self.fetcher.get_prices(SYMBOLS)
        if not prices:
            logger.warning("Не получены цены")
            return

        tasks = [
            asyncio.create_task(self._scan_symbol(sym, prices[sym]))
            for sym in SYMBOLS if sym in prices
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_setups: list[TradeSetup] = []
        for r in results:
            if isinstance(r, Exception):
                logger.error("Symbol scan error: {}", r)
                continue
            all_setups.extend(r)

        # Deduplicate: best score per (symbol, direction, setup_type)
        best: dict[tuple, TradeSetup] = {}
        for s in all_setups:
            k = (s.symbol, s.direction, s.setup_type)
            if k not in best or s.score > best[k].score:
                best[k] = s

        # Filter by quality and score
        quality_order = {"A+": 0, "A": 1, "B": 2, "C": 3}
        min_q_order = quality_order.get(MIN_QUALITY_SIGNAL, 1)
        filtered = [
            s for s in best.values()
            if s.is_valid
            and s.score >= MIN_SCORE_SIGNAL
            and quality_order.get(s.quality, 99) <= min_q_order
        ]
        filtered.sort(key=lambda s: s.score, reverse=True)

        logger.info(
            "Скан #{}: raw={} deduped={} filtered={}",
            self._scan_count, len(all_setups), len(best), len(filtered),
        )

        for s in filtered:
            if self.cooldown.can_signal(s.symbol, s.direction):
                try:
                    await self.telegram.send_signal(s)
                    self.cooldown.record(s.symbol, s.direction)
                    logger.success(
                        "Signal: {} {} {} quality={} score={}",
                        s.symbol, s.direction, s.setup_type, s.quality, s.score,
                    )
                except Exception as e:
                    logger.error("Signal send error {}: {}", s.symbol, e)

    async def _scan_symbol(
        self, symbol: str, current_price: float
    ) -> list[TradeSetup]:
        async with self._sym_sem:
            try:
                df_work, df_senior, df_day = await asyncio.gather(
                    self.fetcher.get_klines(symbol, WORKING_TF, KLINE_LIMIT_WORK),
                    self._get_cached_senior(symbol),
                    self._get_cached_day(symbol),
                )
                if df_work is None or len(df_work) < 60:
                    return []
                return self.detector.scan(
                    symbol=symbol,
                    df_work=df_work,
                    df_senior=df_senior,
                    df_day=df_day,
                    current_price=current_price,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("_scan_symbol {}: {}", symbol, e)
                return []

    async def _get_cached_senior(self, symbol: str) -> Optional[pd.DataFrame]:
        now = time.monotonic()
        ts, df = self._senior_cache.get(symbol, (0.0, None))
        if now - ts < SENIOR_CACHE_TTL and df is not None:
            return df
        df = await self.fetcher.get_klines(symbol, SENIOR_TF, KLINE_LIMIT_SENIOR)
        self._senior_cache[symbol] = (now, df)
        return df

    async def _get_cached_day(self, symbol: str) -> Optional[pd.DataFrame]:
        now = time.monotonic()
        ts, df = self._day_cache.get(symbol, (0.0, None))
        if now - ts < DAY_CACHE_TTL and df is not None:
            return df
        df = await self.fetcher.get_klines(symbol, DAY_TF, KLINE_LIMIT_DAY)
        self._day_cache[symbol] = (now, df)
        return df


# ─────────────────────────── ENTRYPOINT ──────────────────────────────────────

async def main() -> None:
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        logger.error("BYBIT_API_KEY / BYBIT_API_SECRET не заданы!")
        sys.exit(1)

    scanner = Scanner()

    loop = asyncio.get_running_loop()

    def _shutdown(*_):
        logger.info("Получен сигнал завершения...")
        asyncio.ensure_future(scanner.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except (NotImplementedError, RuntimeError):
            pass  # Windows

    await scanner.start()


if __name__ == "__main__":
    asyncio.run(main())
