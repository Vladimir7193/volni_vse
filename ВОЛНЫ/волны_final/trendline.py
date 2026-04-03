"""
Наклонные линии (складные метры) — построение и анализ пробоев.
"""

import numpy as np
from dataclasses import dataclass
from typing import Optional
from loguru import logger


@dataclass
class TrendLine:
    """Наклонная трендовая линия"""
    point1_index: int
    point1_price: float
    point2_index: int
    point2_price: float
    line_type: str              # 'resistance' (для лонга) или 'support' (для шорта)
    slope: float = 0.0          # наклон (цена за бар)
    is_broken: bool = False
    break_price: float = 0.0
    break_index: int = 0
    is_corrected: bool = False  # была ли скорректирована после ложного пробоя

    def __post_init__(self):
        bar_diff = self.point2_index - self.point1_index
        if bar_diff != 0:
            self.slope = (self.point2_price - self.point1_price) / bar_diff
        else:
            self.slope = 0.0

    def price_at_index(self, index: int) -> float:
        """Цена на линии в заданном баре"""
        return self.point1_price + self.slope * (index - self.point1_index)

    def price_at_offset(self, bars_from_point2: int) -> float:
        """Цена на линии через N баров после точки 2"""
        target_index = self.point2_index + bars_from_point2
        return self.price_at_index(target_index)


class TrendLineBuilder:
    """Построение и анализ наклонных линий"""

    def __init__(self, break_threshold_pct: float = 0.1):
        """
        break_threshold_pct: порог пробоя в % (цена должна закрыться
        за линией на этот % чтобы считать пробой истинным)
        """
        self.break_threshold_pct = break_threshold_pct

    def build_resistance_line(
        self,
        wave2_high_price: float,
        wave2_high_index: int,
        wave4_high_price: float,
        wave4_high_index: int
    ) -> TrendLine:
        """
        Строит наклонную линию сопротивления по максимумам волн 2 и 4.
        Используется для ЛОНГ сетапа (пробой вверх = вход).
        """
        return TrendLine(
            point1_index=wave2_high_index,
            point1_price=wave2_high_price,
            point2_index=wave4_high_index,
            point2_price=wave4_high_price,
            line_type='resistance'
        )

    def build_support_line(
        self,
        wave2_low_price: float,
        wave2_low_index: int,
        wave4_low_price: float,
        wave4_low_index: int
    ) -> TrendLine:
        """
        Строит наклонную линию поддержки по минимумам волн 2 и 4.
        Используется для ШОРТ сетапа (пробой вниз = вход).
        """
        return TrendLine(
            point1_index=wave2_low_index,
            point1_price=wave2_low_price,
            point2_index=wave4_low_index,
            point2_price=wave4_low_price,
            line_type='support'
        )

    def build_flag_channel(
        self,
        correction_highs: list[tuple[int, float]],
        correction_lows: list[tuple[int, float]]
    ) -> tuple[Optional[TrendLine], Optional[TrendLine]]:
        """
        Строит канал флага по экстремумам коррекции.
        Возвращает (upper_line, lower_line)
        """
        upper = None
        lower = None

        if len(correction_highs) >= 2:
            h1 = correction_highs[0]
            h2 = correction_highs[-1]
            upper = TrendLine(
                point1_index=h1[0],
                point1_price=h1[1],
                point2_index=h2[0],
                point2_price=h2[1],
                line_type='resistance'
            )

        if len(correction_lows) >= 2:
            l1 = correction_lows[0]
            l2 = correction_lows[-1]
            lower = TrendLine(
                point1_index=l1[0],
                point1_price=l1[1],
                point2_index=l2[0],
                point2_price=l2[1],
                line_type='support'
            )

        return upper, lower

    def check_breakout(
        self,
        trendline: TrendLine,
        candles_high: np.ndarray,
        candles_low: np.ndarray,
        candles_close: np.ndarray,
        start_check_index: int = None
    ) -> Optional[dict]:
        """
        Проверяет, произошёл ли пробой наклонной линии.

        Для resistance (лонг): цена закрытия выше линии
        Для support (шорт): цена закрытия ниже линии

        Возвращает dict с информацией о пробое или None.
        """
        if start_check_index is None:
            start_check_index = trendline.point2_index + 1

        n = len(candles_close)

        for i in range(start_check_index, n):
            line_price = trendline.price_at_index(i)
            close = candles_close[i]
            threshold = line_price * (self.break_threshold_pct / 100)

            if trendline.line_type == 'resistance':
                # Пробой вверх: close > line_price + threshold
                if close > line_price + threshold:
                    return {
                        'broken': True,
                        'break_index': i,
                        'break_price': close,
                        'line_price_at_break': line_price,
                        'direction': 'up'
                    }
            else:
                # Пробой вниз: close < line_price - threshold
                if close < line_price - threshold:
                    return {
                        'broken': True,
                        'break_index': i,
                        'break_price': close,
                        'line_price_at_break': line_price,
                        'direction': 'down'
                    }

        return None

    def check_current_breakout(
        self,
        trendline: TrendLine,
        current_index: int,
        current_close: float
    ) -> Optional[dict]:
        """
        Проверяет пробой на ТЕКУЩЕЙ свече.
        Возвращает dict с 'broken': True/False.
        Возвращает None если цена слишком далеко от линии (>5%) — нерелевантно.
        """
        line_price = trendline.price_at_index(current_index)
        threshold = line_price * (self.break_threshold_pct / 100)

        if trendline.line_type == 'resistance':
            distance_pct = ((current_close - line_price) / line_price) * 100
            if current_close > line_price + threshold:
                return {
                    'broken': True,
                    'break_index': current_index,
                    'break_price': current_close,
                    'line_price_at_break': line_price,
                    'line_price': line_price,
                    'direction': 'up',
                    'distance_pct': distance_pct
                }
        else:
            distance_pct = ((line_price - current_close) / line_price) * 100
            if current_close < line_price - threshold:
                return {
                    'broken': True,
                    'break_index': current_index,
                    'break_price': current_close,
                    'line_price_at_break': line_price,
                    'line_price': line_price,
                    'direction': 'down',
                    'distance_pct': distance_pct
                }

        # Не пробита — вернуть info только если цена близко к линии (< 2%)
        if trendline.line_type == 'resistance':
            approach_pct = ((line_price - current_close) / line_price) * 100
        else:
            approach_pct = ((current_close - line_price) / line_price) * 100

        if approach_pct < 0:
            # Цена по другую сторону, но ниже threshold — ложный пробой / шум
            return None

        if approach_pct > 5.0:
            # Слишком далеко от линии — нерелевантно
            return None

        return {
            'broken': False,
            'line_price': line_price,
            'current_close': current_close,
            'distance_pct': approach_pct
        }

    def correct_line_after_false_break(
        self,
        original_line: TrendLine,
        new_extreme_price: float,
        new_extreme_index: int
    ) -> TrendLine:
        """
        Корректирует наклонную линию после ложного пробоя (например, на 4-й волне).
        Использует точку 1 оригинальной линии и новый экстремум.
        """
        corrected = TrendLine(
            point1_index=original_line.point1_index,
            point1_price=original_line.point1_price,
            point2_index=new_extreme_index,
            point2_price=new_extreme_price,
            line_type=original_line.line_type,
            is_corrected=True
        )
        return corrected
