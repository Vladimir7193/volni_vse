"""
wave_analyzer.py — волновой анализ Эллиотта.

Исправления:
  - _validate_impulse: для UP-импульса добавлено tolerance-предупреждение
    (аналогично DOWN), не просто fail
  - analyze_context: проверяет оба направления, выбирает по силе (без DOWN-bias)
"""
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from loguru import logger
from config import WAVE_RULES


class WaveType(Enum):
    IMPULSE_UP     = "impulse_up"
    IMPULSE_DOWN   = "impulse_down"
    CORRECTION_ABC = "correction_abc"
    DIAGONAL       = "diagonal"
    TRIANGLE       = "triangle"
    FLAT           = "flat"
    ZIGZAG         = "zigzag"
    UNKNOWN        = "unknown"


class WaveDirection(Enum):
    UP   = "up"
    DOWN = "down"


class CorrectionType(Enum):
    SIMPLE  = "simple"
    COMPLEX = "complex"


@dataclass
class Wave:
    number:          str
    start_price:     float
    end_price:       float
    start_index:     int
    end_index:       int
    direction:       WaveDirection
    size:            float = 0.0
    size_pct:        float = 0.0
    bars:            int   = 0
    correction_type: Optional[CorrectionType] = None

    def __post_init__(self):
        self.size = abs(self.end_price - self.start_price)
        if self.start_price > 0:
            self.size_pct = (self.size / self.start_price) * 100
        self.bars = abs(self.end_index - self.start_index)


@dataclass
class WaveCount:
    waves:           list[Wave]    = field(default_factory=list)
    wave_type:       WaveType      = WaveType.UNKNOWN
    direction:       WaveDirection = WaveDirection.UP
    is_valid:        bool          = False
    quality:         str           = "C"      # A+, A, B, C
    alternation_ok:  bool          = False
    proportions_ok:  bool          = False
    rules_ok:        bool          = False
    description:     str           = ""
    context:         str           = ""

    @property
    def wave_count(self) -> int:
        return len(self.waves)

    @property
    def total_size(self) -> float:
        if not self.waves:
            return 0.0
        return abs(self.waves[-1].end_price - self.waves[0].start_price)

    @property
    def total_size_pct(self) -> float:
        if not self.waves or self.waves[0].start_price == 0:
            return 0.0
        return (self.total_size / self.waves[0].start_price) * 100

    @property
    def start_price(self) -> float:
        return self.waves[0].start_price if self.waves else 0.0

    @property
    def end_price(self) -> float:
        return self.waves[-1].end_price if self.waves else 0.0


class WaveAnalyzer:

    def __init__(self):
        self.rules = WAVE_RULES

    # ══════════════════════════════════════════════════════════════════════════
    #  5-волновые импульсы
    # ══════════════════════════════════════════════════════════════════════════

    def find_impulse_waves(
        self,
        legs:       list[dict],
        direction:  WaveDirection = WaveDirection.DOWN,
        min_waves:  int = 5,
    ) -> list[WaveCount]:
        results = []

        if len(legs) < 5:
            return results

        impulse_dir    = 'down' if direction == WaveDirection.DOWN else 'up'
        correction_dir = 'up'   if direction == WaveDirection.DOWN else 'down'

        for start_idx in range(len(legs) - 4):
            candidate_legs = legs[start_idx:start_idx + 5]

            directions_ok = (
                candidate_legs[0]['direction'] == impulse_dir   and
                candidate_legs[1]['direction'] == correction_dir and
                candidate_legs[2]['direction'] == impulse_dir   and
                candidate_legs[3]['direction'] == correction_dir and
                candidate_legs[4]['direction'] == impulse_dir
            )
            if not directions_ok:
                continue

            waves = self._create_waves_from_legs(candidate_legs, direction)
            if not waves or len(waves) != 5:
                continue

            wave_count = WaveCount(
                waves      = waves,
                wave_type  = WaveType.IMPULSE_DOWN if direction == WaveDirection.DOWN
                             else WaveType.IMPULSE_UP,
                direction  = direction,
            )
            self._validate_impulse(wave_count)

            if wave_count.rules_ok:
                self._assess_quality(wave_count)
                results.append(wave_count)

        quality_order = {"A+": 0, "A": 1, "B": 2, "C": 3}
        results.sort(key=lambda wc: quality_order.get(wc.quality, 4))
        return results

    # ══════════════════════════════════════════════════════════════════════════
    #  ABC коррекции
    # ══════════════════════════════════════════════════════════════════════════

    def find_abc_correction(
        self,
        legs:      list[dict],
        direction: WaveDirection = WaveDirection.UP,
    ) -> list[WaveCount]:
        results = []

        if len(legs) < 3:
            return results

        abc_dirs = ['up', 'down', 'up'] if direction == WaveDirection.UP else ['down', 'up', 'down']

        for start_idx in range(len(legs) - 2):
            candidate = legs[start_idx:start_idx + 3]

            dirs_ok = all(candidate[i]['direction'] == abc_dirs[i] for i in range(3))
            if not dirs_ok:
                continue

            waves  = []
            labels = ["A", "B", "C"]
            for i, leg in enumerate(candidate):
                w_dir = WaveDirection.UP if leg['direction'] == 'up' else WaveDirection.DOWN
                waves.append(Wave(
                    number      = labels[i],
                    start_price = leg['start']['price'],
                    end_price   = leg['end']['price'],
                    start_index = leg['start']['bar_index'],
                    end_index   = leg['end']['bar_index'],
                    direction   = w_dir,
                ))

            wc = WaveCount(
                waves      = waves,
                wave_type  = WaveType.CORRECTION_ABC,
                direction  = direction,
                is_valid   = True,
                quality    = "B",
            )

            a_size = waves[0].size
            c_size = waves[2].size
            if a_size > 0:
                c_to_a = c_size / a_size
                if 0.618 <= c_to_a <= 1.618:
                    wc.quality = "A"
                if 0.9 <= c_to_a <= 1.1:
                    wc.quality = "A+"

            wc.description = self._describe_abc(waves)
            results.append(wc)

        return results

    # ══════════════════════════════════════════════════════════════════════════
    #  Вспомогательные методы
    # ══════════════════════════════════════════════════════════════════════════

    def _create_waves_from_legs(
        self, legs: list[dict], direction: WaveDirection
    ) -> list[Wave]:
        waves  = []
        labels = ["1", "2", "3", "4", "5"]

        for i, leg in enumerate(legs):
            w_dir     = WaveDirection.UP if leg['direction'] == 'up' else WaveDirection.DOWN
            corr_type = None
            if labels[i] in ("2", "4"):
                corr_type = CorrectionType.SIMPLE if leg['bars'] <= 5 else CorrectionType.COMPLEX

            waves.append(Wave(
                number          = labels[i],
                start_price     = leg['start']['price'],
                end_price       = leg['end']['price'],
                start_index     = leg['start']['bar_index'],
                end_index       = leg['end']['bar_index'],
                direction       = w_dir,
                correction_type = corr_type,
            ))
        return waves

    def _validate_impulse(self, wc: WaveCount):
        """
        Валидация правил Эллиотта.
        ИСПРАВЛЕНО: для UP импульса добавлен tolerance-допуск (как в DOWN).
        """
        wc.description = ""
        waves = wc.waves
        if len(waves) != 5:
            wc.rules_ok = False
            return

        w1, w2, w3, w4, w5 = waves

        # Правило 1: волна 2 не откатывается за начало волны 1
        if wc.direction == WaveDirection.DOWN:
            if w2.end_price > w1.start_price:
                wc.rules_ok = False
                wc.description = "❌ Волна 2 превышает начало волны 1"
                return
        else:
            if w2.end_price < w1.start_price:
                wc.rules_ok = False
                wc.description = "❌ Волна 2 опускается ниже начала волны 1"
                return

        # Правило 2: волна 3 не самая короткая
        if w3.size < w1.size and w3.size < w5.size:
            wc.rules_ok = False
            wc.description = "❌ Волна 3 — самая короткая"
            return

        # Правило 3: волна 4 не заходит в территорию волны 1 (с допуском 5%)
        if wc.direction == WaveDirection.DOWN:
            if w4.end_price > w1.end_price:
                overlap_pct = ((w4.end_price - w1.end_price) / w1.size) * 100 if w1.size > 0 else 100
                if overlap_pct > 5:
                    wc.rules_ok = False
                    wc.description = "❌ Волна 4 заходит в территорию волны 1"
                    return
                else:
                    wc.description += "⚠️ Небольшое перекрытие W4-W1 (возможна диагональ). "
        else:  # UP
            if w4.end_price < w1.end_price:
                overlap_pct = ((w1.end_price - w4.end_price) / w1.size) * 100 if w1.size > 0 else 100
                if overlap_pct > 5:
                    wc.rules_ok = False
                    wc.description = "❌ Волна 4 заходит в территорию волны 1"
                    return
                else:
                    # ИСПРАВЛЕНО: добавлено tolerance-предупреждение для UP (было пусто)
                    wc.description += "⚠️ Небольшое перекрытие W4-W1 (возможна диагональ). "

        wc.rules_ok = True

        # Чередование
        if w2.correction_type and w4.correction_type:
            wc.alternation_ok = (w2.correction_type != w4.correction_type)
        else:
            wc.alternation_ok = (w4.bars != w2.bars) if (w2.bars > 0 and w4.bars > 0) else True

        # Пропорции
        if w1.size > 0:
            w3_ext         = w3.size / w1.size
            wc.proportions_ok = (w3_ext >= self.rules["wave3_min_extension"])

            if self.rules["wave3_typical_ext_min"] <= w3_ext <= self.rules["wave3_typical_ext_max"]:
                wc.description += f"Волна 3 = {w3_ext:.1%} от W1 (идеально). "
            elif w3_ext >= self.rules["wave3_min_extension"]:
                wc.description += f"Волна 3 = {w3_ext:.1%} от W1 (допустимо). "
        else:
            wc.proportions_ok = False

    def _assess_quality(self, wc: WaveCount):
        score = 0
        if wc.rules_ok:        score += 3
        if wc.alternation_ok:  score += 2
        if wc.proportions_ok:  score += 2
        if wc.total_size_pct >= 3.0: score += 1
        if wc.total_size_pct >= 5.0: score += 1

        w1, w5 = wc.waves[0], wc.waves[4]
        if w1.size > 0:
            w5_to_w1 = w5.size / w1.size
            if 0.618 <= w5_to_w1 <= 1.618:
                score += 1

        wc.quality  = "A+" if score >= 9 else "A" if score >= 7 else "B" if score >= 5 else "C"
        wc.is_valid = True
        wc.description += self._describe_impulse(wc)

    def _describe_impulse(self, wc: WaveCount) -> str:
        dir_sym = "↓" if wc.direction == WaveDirection.DOWN else "↑"
        opp_sym = "↑" if wc.direction == WaveDirection.DOWN else "↓"
        parts   = []
        for w in wc.waves:
            sym      = dir_sym if w.number in ("1", "3", "5") else opp_sym
            corr_str = f" ({w.correction_type.value})" if w.correction_type else ""
            parts.append(f"W{w.number}{sym} ${w.start_price:.2f}→${w.end_price:.2f}{corr_str}")
        return " | ".join(parts)

    def _describe_abc(self, waves: list[Wave]) -> str:
        parts = []
        for w in waves:
            sym = "↑" if w.direction == WaveDirection.UP else "↓"
            parts.append(f"W{w.number}{sym} ${w.start_price:.2f}→${w.end_price:.2f}")
        return " | ".join(parts)

    # ══════════════════════════════════════════════════════════════════════════
    #  Контекст старшего ТФ (без DOWN-bias)
    # ══════════════════════════════════════════════════════════════════════════

    def analyze_context(
        self,
        senior_legs:          list[dict],
        working_wave_count:   Optional[WaveCount],
    ) -> str:
        """
        ИСПРАВЛЕНО: ищет 5-волновые паттерны в ОБОИХ направлениях,
        возвращает тот, у которого выше качество (не всегда DOWN).
        """
        if not senior_legs or len(senior_legs) < 3:
            return "Недостаточно данных на старшем ТФ"

        # Ищем в обоих направлениях, выбираем лучшее
        best_impulse     = None
        best_impulse_dir = None
        quality_order    = {"A+": 0, "A": 1, "B": 2, "C": 3}

        for direction in [WaveDirection.DOWN, WaveDirection.UP]:
            impulses = self.find_impulse_waves(senior_legs, direction)
            if impulses:
                candidate = impulses[0]
                if best_impulse is None or (
                    quality_order.get(candidate.quality, 4) <
                    quality_order.get(best_impulse.quality, 4)
                ):
                    best_impulse     = candidate
                    best_impulse_dir = direction

        if best_impulse:
            dir_str = "вниз" if best_impulse_dir == WaveDirection.DOWN else "вверх"
            return (
                f"На старшем ТФ: 5 волн {dir_str} "
                f"(качество {best_impulse.quality}). "
                f"Ожидается коррекция/разворот."
            )

        # Ищем ABC
        best_abc     = None
        best_abc_dir = None
        for direction in [WaveDirection.UP, WaveDirection.DOWN]:
            abcs = self.find_abc_correction(senior_legs, direction)
            if abcs:
                candidate = abcs[0]
                if best_abc is None or (
                    quality_order.get(candidate.quality, 4) <
                    quality_order.get(best_abc.quality, 4)
                ):
                    best_abc     = candidate
                    best_abc_dir = direction

        if best_abc:
            dir_str = "вверх" if best_abc_dir == WaveDirection.UP else "вниз"
            return (
                f"На старшем ТФ: ABC коррекция {dir_str} "
                f"(качество {best_abc.quality}). "
                f"Возможно завершение коррекции."
            )

        # Общий тренд
        last_legs  = senior_legs[-5:]
        up_size    = sum(l['size'] for l in last_legs if l['direction'] == 'up')
        down_size  = sum(l['size'] for l in last_legs if l['direction'] == 'down')

        if up_size > down_size * 1.5:
            return "На старшем ТФ: восходящий тренд (без чёткой волновой структуры)"
        elif down_size > up_size * 1.5:
            return "На старшем ТФ: нисходящий тренд (без чёткой волновой структуры)"
        else:
            return "На старшем ТФ: боковик / неопределённость"
