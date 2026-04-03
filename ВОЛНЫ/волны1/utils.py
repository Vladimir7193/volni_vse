"""
Утилиты и вспомогательные функции
"""

import time
from datetime import datetime, timezone
from loguru import logger
import numpy as np
import pandas as pd


def timestamp_to_datetime(ts_ms: int) -> datetime:
    """Конвертация timestamp в миллисекундах в datetime UTC"""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def datetime_to_timestamp(dt: datetime) -> int:
    """Конвертация datetime в timestamp миллисекунды"""
    return int(dt.timestamp() * 1000)


def format_price(price: float, decimals: int = None) -> str:
    """Форматирование цены"""
    if price is None:
        return "N/A"
    if decimals is None:
        if price >= 10000:
            decimals = 1
        elif price >= 100:
            decimals = 2
        elif price >= 1:
            decimals = 3
        elif price >= 0.01:
            decimals = 5
        elif price >= 0.0001:
            decimals = 6
        else:
            decimals = 8
    return f"{price:.{decimals}f}"


def format_pct(value: float) -> str:
    """Форматирование процентов"""
    if value is None:
        return "N/A"
    return f"{value:+.2f}%"


def pct_change(price_from: float, price_to: float) -> float:
    """Процентное изменение"""
    if price_from == 0:
        return 0.0
    return ((price_to - price_from) / price_from) * 100


def fib_level(high: float, low: float, level: float, direction: str = "up") -> float:
    """
    Расчёт уровня Фибоначчи
    direction='up': коррекция вверх от low (после падения high->low)
    direction='down': коррекция вниз от high (после роста low->high)
    """
    diff = high - low
    if direction == "up":
        return low + diff * level
    else:
        return high - diff * level


def calculate_position_size(
    risk_pct: float,
    stop_loss_pct: float,
    max_position_pct: float = 100.0
) -> float:
    """
    Расчёт размера позиции
    risk_pct: допустимый убыток в % от депозита
    stop_loss_pct: размер стоп-лосса в %
    Возвращает: % от депозита для входа
    """
    if stop_loss_pct <= 0:
        return 0.0
    size = (risk_pct / stop_loss_pct) * 100
    return min(size, max_position_pct)


def calculate_rr_ratio(
    entry: float,
    stop_loss: float,
    take_profit: float
) -> float:
    """Расчёт соотношения Risk:Reward"""
    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    if risk == 0:
        return 0.0
    return reward / risk


def weighted_rr_ratio(
    entry: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp1_pct: float = 0.7,
    tp2_pct: float = 0.3
) -> float:
    """Средневзвешенный R:R"""
    risk = abs(entry - stop_loss)
    if risk == 0:
        return 0.0
    reward1 = abs(tp1 - entry) * tp1_pct
    reward2 = abs(tp2 - entry) * tp2_pct
    avg_reward = reward1 + reward2
    return avg_reward / risk


class RateLimiter:
    """Ограничитель частоты запросов"""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.calls = []

    async def acquire(self):
        now = time.time()
        self.calls = [c for c in self.calls if now - c < self.period]
        if len(self.calls) >= self.max_calls:
            sleep_time = self.period - (now - self.calls[0])
            if sleep_time > 0:
                import asyncio
                await asyncio.sleep(sleep_time)
        self.calls.append(time.time())


class SignalCooldown:
    """Кулдаун сигналов по монетам"""

    def __init__(self, cooldown_minutes: int = 30):
        self.cooldown_seconds = cooldown_minutes * 60
        self.last_signals: dict[str, float] = {}

    def can_signal(self, symbol: str, timeframe: str) -> bool:
        key = f"{symbol}_{timeframe}"
        now = time.time()
        last = self.last_signals.get(key, 0)
        return (now - last) >= self.cooldown_seconds

    def record_signal(self, symbol: str, timeframe: str):
        key = f"{symbol}_{timeframe}"
        self.last_signals[key] = time.time()
