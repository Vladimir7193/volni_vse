"""
setups.py — детектор торговых сетапов №1 (5 волн + наклонка) и №2 (Флаг + пробой).

Исправления:
  - Setup 2 SHORT: fib_levels направлены вниз (были вверх — ошибка)
  - Setup 2: break → return None при rejection (больше не ищет слабый 3-ногий паттерн)
  - calculate_position_size использует risk_manager (не дублирует логику)
"""
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger
import numpy as np

from wave_analyzer import WaveAnalyzer, WaveCount, WaveDirection, WaveType
from trendline import TrendLine, TrendLineBuilder
from indicators import get_zigzag_for_timeframe, find_zigzag_legs
from config import WAVE_RULES, MIN_MOVE_PCT, MAX_STOP_LOSS_PCT, MIN_RR_RATIO, RISK_PER_TRADE_PCT
from utils import (
    fib_level, pct_change, calculate_rr_ratio,
    weighted_rr_ratio, calculate_position_size, format_price, format_pct,
)


@dataclass
class TradeSetup:
    setup_type:    str
    setup_name:    str
    symbol:        str
    timeframe:     str
    direction:     str                  # "LONG" | "SHORT"

    wave_count:    Optional[WaveCount] = None
    wave_description:        str = ""
    alternation_description: str = ""
    senior_context:          str = ""

    trendline:             Optional[TrendLine] = None
    trendline_broken:      bool  = False
    trendline_break_price: float = 0.0

    entry_price:    float = 0.0
    stop_loss:      float = 0.0
    stop_loss_pct:  float = 0.0
    stop_reason:    str   = ""

    tp1_price:     float = 0.0
    tp1_pct:       float = 0.0
    tp1_fib_level: str   = ""
    tp2_price:     float = 0.0
    tp2_pct:       float = 0.0
    tp2_fib_level: str   = ""

    rr_to_tp1:    float = 0.0
    rr_weighted:  float = 0.0

    fib_levels: dict  = field(default_factory=dict)
    fib_from:   float = 0.0
    fib_to:     float = 0.0

    wave_equality_price: float = 0.0
    wave3_ext_161:       float = 0.0
    wave3_ext_227:       float = 0.0

    position_size_pct: float = 0.0
    risk_per_trade_pct: float = RISK_PER_TRADE_PCT

    quality:          str  = "C"
    is_valid:         bool = False
    rejection_reason: str  = ""

    status:     str = "pending"
    re_entries: int = 0


class SetupDetector:

    def __init__(self):
        self.wave_analyzer      = WaveAnalyzer()
        self.trendline_builder  = TrendLineBuilder(break_threshold_pct=0.1)

    # ══════════════════════════════════════════════════════════════════════════

    def scan_for_setups(
        self,
        symbol:            str,
        df_working,
        timeframe_working: str,
        df_senior          = None,
        timeframe_senior:  str   = "240",
        current_price:     float = 0.0,
    ) -> list[TradeSetup]:
        setups = []

        if df_working is None or len(df_working) < 50:
            return setups

        pivots_w, legs_w = get_zigzag_for_timeframe(df_working, timeframe_working)
        if len(legs_w) < 5:
            return setups

        # Контекст старшего ТФ
        senior_context         = ""
        senior_direction_hint  = None
        if df_senior is not None and len(df_senior) > 30:
            pivots_s, legs_s = get_zigzag_for_timeframe(df_senior, timeframe_senior)
            if legs_s:
                senior_context = self.wave_analyzer.analyze_context(legs_s, None)
                if "5 волн вниз" in senior_context:
                    senior_direction_hint = "LONG"
                elif "5 волн вверх" in senior_context:
                    senior_direction_hint = "SHORT"

        for direction in [WaveDirection.DOWN, WaveDirection.UP]:
            setup = self._detect_setup_1(
                symbol=symbol, legs=legs_w, df=df_working,
                timeframe=timeframe_working, direction=direction,
                senior_context=senior_context,
                senior_direction_hint=senior_direction_hint,
                current_price=current_price,
            )
            if setup and setup.is_valid:
                setups.append(setup)

        for direction in [WaveDirection.UP, WaveDirection.DOWN]:
            setup = self._detect_setup_2(
                symbol=symbol, legs=legs_w, pivots=pivots_w, df=df_working,
                timeframe=timeframe_working, direction=direction,
                senior_context=senior_context,
                senior_direction_hint=senior_direction_hint,
                current_price=current_price,
            )
            if setup and setup.is_valid:
                setups.append(setup)

        return setups

    # ══════════════════════════════════════════════════════════════════════════
    #  Сетап №1: 5 волн + наклонка
    # ══════════════════════════════════════════════════════════════════════════

    def _detect_setup_1(
        self,
        symbol:               str,
        legs:                 list[dict],
        df,
        timeframe:            str,
        direction:            WaveDirection,
        senior_context:       str,
        senior_direction_hint: Optional[str],
        current_price:        float,
    ) -> Optional[TradeSetup]:

        impulses = self.wave_analyzer.find_impulse_waves(legs, direction)
        if not impulses:
            return None

        best_wc   = impulses[0]
        trade_dir = "LONG" if direction == WaveDirection.DOWN else "SHORT"

        # Понижаем качество если контртренд
        if senior_direction_hint and senior_direction_hint != trade_dir:
            quality_map = {"A+": "B", "A": "B", "B": "C"}
            best_wc.quality = quality_map.get(best_wc.quality, "C")

        waves        = best_wc.waves
        w1, w2, w3, w4, w5 = waves

        # Строим наклонную линию
        if trade_dir == "LONG":
            trendline = self.trendline_builder.build_resistance_line(
                wave2_high_price=w2.end_price, wave2_high_index=w2.end_index,
                wave4_high_price=w4.end_price, wave4_high_index=w4.end_index,
            )
        else:
            trendline = self.trendline_builder.build_support_line(
                wave2_low_price=w2.end_price, wave2_low_index=w2.end_index,
                wave4_low_price=w4.end_price, wave4_low_index=w4.end_index,
            )

        current_index = len(df) - 1
        breakout = self.trendline_builder.check_current_breakout(
            trendline=trendline,
            current_index=current_index,
            current_close=current_price,
        )
        if breakout is None:
            return None

        setup = TradeSetup(
            setup_type   = "setup_1",
            setup_name   = '№1 «5 волн + наклонка»',
            symbol       = symbol,
            timeframe    = timeframe,
            direction    = trade_dir,
            wave_count   = best_wc,
            wave_description = best_wc.description,
            senior_context   = senior_context,
            trendline        = trendline,
        )

        w2_type = w2.correction_type.value if w2.correction_type else "unknown"
        w4_type = w4.correction_type.value if w4.correction_type else "unknown"
        alt_ok  = "✅" if best_wc.alternation_ok else "⚠️"
        setup.alternation_description = f"Волна 2 = {w2_type} → Волна 4 = {w4_type} {alt_ok}"

        if breakout['broken']:
            setup.trendline_broken     = True
            setup.trendline_break_price = breakout['break_price']
            setup.entry_price          = breakout['break_price']
        else:
            setup.trendline_broken = False
            setup.entry_price      = breakout['line_price']
            setup.status           = "forming"

        # Стоп-лосс
        if trade_dir == "LONG":
            setup.stop_loss   = w5.end_price * 0.998
            setup.stop_reason = "Под минимум волны 5"
        else:
            setup.stop_loss   = w5.end_price * 1.002
            setup.stop_reason = "Над максимум волны 5"

        setup.stop_loss_pct = abs(pct_change(setup.entry_price, setup.stop_loss))

        # Fibonacci уровни и TP
        impulse_start = w1.start_price
        impulse_end   = w5.end_price

        setup.fib_from = impulse_start
        setup.fib_to   = impulse_end

        if trade_dir == "LONG":
            # Коррекция вверх от падения (high=impulse_start, low=impulse_end)
            for level in [0.236, 0.382, 0.500, 0.618, 0.764]:
                setup.fib_levels[level] = fib_level(impulse_start, impulse_end, level, "up")
            setup.tp1_price     = setup.fib_levels[0.382]
            setup.tp1_fib_level = "38.2%"
            setup.tp2_price     = setup.fib_levels[0.618]
            setup.tp2_fib_level = "61.8%"
        else:
            # Коррекция вниз от роста (low=impulse_start, high=impulse_end)
            for level in [0.236, 0.382, 0.500, 0.618, 0.764]:
                setup.fib_levels[level] = fib_level(impulse_end, impulse_start, level, "down")
            setup.tp1_price     = setup.fib_levels[0.382]
            setup.tp1_fib_level = "38.2%"
            setup.tp2_price     = setup.fib_levels[0.618]
            setup.tp2_fib_level = "61.8%"

        setup.tp1_pct = abs(pct_change(setup.entry_price, setup.tp1_price))
        setup.tp2_pct = abs(pct_change(setup.entry_price, setup.tp2_price))

        setup.rr_to_tp1  = calculate_rr_ratio(setup.entry_price, setup.stop_loss, setup.tp1_price)
        setup.rr_weighted = weighted_rr_ratio(
            setup.entry_price, setup.stop_loss, setup.tp1_price, setup.tp2_price
        )

        # Дополнительные уровни
        if trade_dir == "LONG":
            w1_size              = abs(setup.tp1_price - w5.end_price)
            setup.wave3_ext_161  = w5.end_price + w1_size * 1.618
            setup.wave3_ext_227  = w5.end_price + w1_size * 2.272
        else:
            w1_size              = abs(w5.end_price - setup.tp1_price)
            setup.wave3_ext_161  = w5.end_price - w1_size * 1.618
            setup.wave3_ext_227  = w5.end_price - w1_size * 2.272

        setup.position_size_pct = calculate_position_size(
            risk_pct       = setup.risk_per_trade_pct,
            stop_loss_pct  = setup.stop_loss_pct,
        )
        setup.quality  = best_wc.quality

        # Фильтры
        setup.is_valid        = True
        setup.rejection_reason = ""

        if setup.stop_loss_pct > MAX_STOP_LOSS_PCT:
            setup.is_valid         = False
            setup.rejection_reason = f"Стоп {setup.stop_loss_pct:.1f}% > {MAX_STOP_LOSS_PCT}%"
        elif setup.rr_weighted < MIN_RR_RATIO:
            setup.is_valid         = False
            setup.rejection_reason = f"R:R {setup.rr_weighted:.1f} < {MIN_RR_RATIO}"
        elif setup.tp1_pct < MIN_MOVE_PCT:
            setup.is_valid         = False
            setup.rejection_reason = f"Потенциал {setup.tp1_pct:.1f}% < {MIN_MOVE_PCT}%"
        elif setup.quality == "C":
            setup.position_size_pct = min(setup.position_size_pct, 15.0)

        return setup

    # ══════════════════════════════════════════════════════════════════════════
    #  Сетап №2: Флаг + пробой
    # ══════════════════════════════════════════════════════════════════════════

    def _detect_setup_2(
        self,
        symbol:               str,
        legs:                 list[dict],
        pivots:               list[dict],
        df,
        timeframe:            str,
        direction:            WaveDirection,
        senior_context:       str,
        senior_direction_hint: Optional[str],
        current_price:        float,
    ) -> Optional[TradeSetup]:

        if len(legs) < 6:
            return None

        if direction == WaveDirection.UP:
            trade_dir      = "LONG"
            impulse_dir    = 'up'
            correction_dir = 'down'
        else:
            trade_dir      = "SHORT"
            impulse_dir    = 'down'
            correction_dir = 'up'

        for impulse_len in [5, 3]:
            for corr_start in range(impulse_len, len(legs) - 2):
                impulse_legs = legs[corr_start - impulse_len:corr_start]
                impulse_ok   = all(
                    leg['direction'] == (impulse_dir if i % 2 == 0 else correction_dir)
                    for i, leg in enumerate(impulse_legs)
                )
                if not impulse_ok:
                    continue

                if corr_start + 3 > len(legs):
                    continue

                corr_legs   = legs[corr_start:corr_start + 3]
                corr_dirs_ok = (
                    corr_legs[0]['direction'] == correction_dir and
                    corr_legs[1]['direction'] == impulse_dir    and
                    corr_legs[2]['direction'] == correction_dir
                )
                if not corr_dirs_ok:
                    continue

                impulse_start = impulse_legs[0]['start']['price']
                impulse_end   = impulse_legs[-1]['end']['price']
                impulse_size  = abs(impulse_end - impulse_start)

                corr_end   = corr_legs[-1]['end']['price']
                corr_depth = abs(corr_end - impulse_end)

                if impulse_size == 0:
                    continue

                corr_ratio = corr_depth / impulse_size
                if not (0.25 <= corr_ratio <= 0.75):
                    continue

                # Строим канал флага
                corr_highs = []
                corr_lows  = []
                for leg in corr_legs:
                    if leg['direction'] == 'up':
                        corr_highs.append((leg['end']['bar_index'],   leg['end']['price']))
                        corr_lows.append( (leg['start']['bar_index'], leg['start']['price']))
                    else:
                        corr_highs.append((leg['start']['bar_index'], leg['start']['price']))
                        corr_lows.append( (leg['end']['bar_index'],   leg['end']['price']))

                upper_line, lower_line = self.trendline_builder.build_flag_channel(
                    corr_highs, corr_lows
                )

                if trade_dir == "LONG"  and upper_line is None: continue
                if trade_dir == "SHORT" and lower_line is None: continue

                check_line    = upper_line if trade_dir == "LONG" else lower_line
                current_index = len(df) - 1

                breakout = self.trendline_builder.check_current_breakout(
                    trendline     = check_line,
                    current_index = current_index,
                    current_close = current_price,
                )
                if not breakout:
                    continue

                setup = TradeSetup(
                    setup_type     = "setup_2",
                    setup_name     = '№2 «Флаг + пробой»',
                    symbol         = symbol,
                    timeframe      = timeframe,
                    direction      = trade_dir,
                    senior_context = senior_context,
                    trendline      = check_line,
                )
                setup.wave_description = (
                    f"Импульс {'вверх' if trade_dir == 'LONG' else 'вниз'} "
                    f"({impulse_len} ног) + ABC коррекция "
                    f"(глубина {corr_ratio:.0%})"
                )

                if breakout['broken']:
                    setup.trendline_broken      = True
                    setup.trendline_break_price = breakout['break_price']
                    setup.entry_price           = breakout['break_price']
                else:
                    setup.trendline_broken = False
                    setup.entry_price      = breakout.get('line_price', current_price)
                    setup.status           = "forming"

                # Стоп-лосс
                if trade_dir == "LONG":
                    if not corr_lows:
                        continue
                    flag_low      = min(l[1] for l in corr_lows)
                    setup.stop_loss   = flag_low * 0.998
                    setup.stop_reason = "Под минимум флага (волна C)"
                else:
                    if not corr_highs:
                        continue
                    flag_high     = max(h[1] for h in corr_highs)
                    setup.stop_loss   = flag_high * 1.002
                    setup.stop_reason = "Над максимум флага (волна C)"

                setup.stop_loss_pct = abs(pct_change(setup.entry_price, setup.stop_loss))

                # TP: проекция импульса
                if trade_dir == "LONG":
                    setup.tp1_price         = setup.entry_price + impulse_size * 0.618
                    setup.tp2_price         = setup.entry_price + impulse_size
                    setup.wave_equality_price = setup.entry_price + impulse_size
                else:
                    setup.tp1_price         = setup.entry_price - impulse_size * 0.618
                    setup.tp2_price         = setup.entry_price - impulse_size
                    setup.wave_equality_price = setup.entry_price - impulse_size

                setup.tp1_pct     = abs(pct_change(setup.entry_price, setup.tp1_price))
                setup.tp2_pct     = abs(pct_change(setup.entry_price, setup.tp2_price))
                setup.tp1_fib_level = "61.8% от импульса"
                setup.tp2_fib_level = "100% (равенство волн)"

                # Фибоначчи уровни (для отображения)
                setup.fib_from = impulse_start
                setup.fib_to   = impulse_end
                for level in [0.382, 0.500, 0.618]:
                    if trade_dir == "LONG":
                        # Откат бычьего импульса: уровни поддержки ниже entry
                        setup.fib_levels[level] = fib_level(impulse_end, impulse_start, level, "down")
                    else:
                        # ИСПРАВЛЕНО: откат медвежьего импульса = уровни поддержки ВЫШЕ entry (сопротивления)
                        # fib_level(high, low, level, "down") = high - (high-low)*level
                        # Для SHORT: high = impulse_start (вершина до падения),
                        #            low  = impulse_end   (дно после падения)
                        setup.fib_levels[level] = fib_level(impulse_start, impulse_end, level, "down")

                setup.rr_to_tp1   = calculate_rr_ratio(setup.entry_price, setup.stop_loss, setup.tp1_price)
                setup.rr_weighted = weighted_rr_ratio(
                    setup.entry_price, setup.stop_loss, setup.tp1_price, setup.tp2_price
                )
                setup.position_size_pct = calculate_position_size(
                    risk_pct      = setup.risk_per_trade_pct,
                    stop_loss_pct = setup.stop_loss_pct,
                )

                # Качество
                setup.quality = "B"
                if senior_direction_hint == trade_dir:
                    setup.quality = "A"
                if 0.35 <= corr_ratio <= 0.65 and setup.quality == "A":
                    setup.quality = "A+"

                # Фильтры
                setup.is_valid         = True
                setup.rejection_reason = ""

                if setup.stop_loss_pct > MAX_STOP_LOSS_PCT:
                    setup.is_valid         = False
                    setup.rejection_reason = f"Стоп {setup.stop_loss_pct:.1f}% > {MAX_STOP_LOSS_PCT}%"
                elif setup.rr_weighted < MIN_RR_RATIO:
                    setup.is_valid         = False
                    setup.rejection_reason = f"R:R {setup.rr_weighted:.1f} < {MIN_RR_RATIO}"
                elif setup.tp1_pct < MIN_MOVE_PCT:
                    setup.is_valid         = False
                    setup.rejection_reason = f"Потенциал {setup.tp1_pct:.1f}% < {MIN_MOVE_PCT}%"

                if setup.is_valid:
                    return setup

                if setup.rejection_reason:
                    # ИСПРАВЛЕНО: return None вместо break
                    # Не ищем более слабый 3-ногий паттерн если 5-ногой не прошёл фильтры
                    return None

        return None
