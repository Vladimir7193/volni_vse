"""
position_tracker.py — трекер активных позиций.

Исправления:
  - TP2 проверяется ТОЛЬКО после tp1_hit (был баг: TP2 срабатывал без TP1)
  - Трейлинг-стоп хранит реальные локальные экстремумы, не тиковые цены
  - Размер trailing_lows/highs ограничен (нет утечки памяти)
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field
from loguru import logger

from risk_manager import RiskManager, TradeRecord
from setups import TradeSetup
from trendline import TrendLine, TrendLineBuilder
from utils import pct_change, format_price, format_pct

# Минимальный шаг для регистрации нового экстремума (% от цены)
_TRAILING_PIVOT_STEP_PCT = 0.20
_TRAILING_MAX_POINTS     = 50   # ограничение памяти


@dataclass
class ActivePosition:
    setup:             TradeSetup
    trade:             TradeRecord
    trailing_trendline: Optional[TrendLine] = None

    # Для лонга: список (price, timestamp) подтверждённых локальных минимумов
    # Для шорта: список (price, timestamp) локальных максимумов
    trailing_pivots: list = field(default_factory=list)

    # Предыдущая цена для сравнения (обнаружение экстремумов)
    _prev_price:    float = 0.0
    _prev_prev_price: float = 0.0

    updates_sent: list = field(default_factory=list)


class PositionTracker:

    def __init__(self, risk_manager: RiskManager):
        self.risk_manager     = risk_manager
        self.positions:       dict[str, ActivePosition] = {}
        self.trendline_builder = TrendLineBuilder()

    # ──────────────────────────────────────────────────────────────────────────
    def add_position(self, setup: TradeSetup, trade: TradeRecord):
        position = ActivePosition(setup=setup, trade=trade)
        self.positions[setup.symbol] = position
        logger.info(f"Позиция добавлена: {setup.symbol} {trade.direction}")

    # ──────────────────────────────────────────────────────────────────────────
    def check_positions(self, prices: dict[str, float]) -> list[dict]:
        """
        Порядок проверок:
          1. Stop Loss
          2. TP1 (если ещё не достигнут)
          3. TP2 — ТОЛЬКО если tp1_hit (ИСПРАВЛЕНО)
          4. Trailing stop (ТОЛЬКО после tp1_hit, по реальным пивотам)
        """
        updates = []

        for symbol, pos in list(self.positions.items()):
            if symbol not in prices:
                continue

            current_price = prices[symbol]
            trade         = pos.trade
            setup         = pos.setup

            # ── 1. Stop Loss ──────────────────────────────────────────────────
            stop_hit = (
                current_price <= trade.stop_loss if trade.direction == "LONG"
                else current_price >= trade.stop_loss
            )
            if stop_hit:
                closed = self.risk_manager.close_trade(symbol, current_price, "stopped")
                if closed:
                    updates.append({
                        "type": "stop_hit", "symbol": symbol,
                        "trade": closed, "setup": setup, "price": current_price,
                    })
                del self.positions[symbol]
                continue

            # ── 2. TP1 ────────────────────────────────────────────────────────
            if not trade.tp1_hit:
                tp1_hit = (
                    current_price >= setup.tp1_price if trade.direction == "LONG"
                    else current_price <= setup.tp1_price
                )
                if tp1_hit:
                    updated = self.risk_manager.update_tp1_hit(symbol, current_price)
                    if updated:
                        updates.append({
                            "type": "tp1_hit", "symbol": symbol,
                            "trade": updated, "setup": setup, "price": current_price,
                        })
                    # Продолжаем — TP2 проверим в следующем тике (tp1_hit уже True)
                    self._update_trailing(pos, current_price)
                    continue

            # ── 3. TP2 — только после TP1 (ИСПРАВЛЕНО) ───────────────────────
            if trade.tp1_hit:
                tp2_hit = (
                    current_price >= setup.tp2_price if trade.direction == "LONG"
                    else current_price <= setup.tp2_price
                )
                if tp2_hit:
                    closed = self.risk_manager.close_trade(symbol, current_price, "tp2_hit")
                    if closed:
                        updates.append({
                            "type": "tp2_hit", "symbol": symbol,
                            "trade": closed, "setup": setup, "price": current_price,
                        })
                    del self.positions[symbol]
                    continue

                # ── 4. Trailing Stop ──────────────────────────────────────────
                self._update_trailing(pos, current_price)

                if pos.trailing_trendline and len(pos.trailing_pivots) >= 2:
                    n          = len(pos.trailing_pivots)
                    line_price = pos.trailing_trendline.price_at_index(n - 1)

                    trailing_broken = (
                        current_price < line_price if trade.direction == "LONG"
                        else current_price > line_price
                    )
                    if trailing_broken:
                        closed = self.risk_manager.close_trade(
                            symbol, current_price, "trailing_stop"
                        )
                        if closed:
                            updates.append({
                                "type": "trailing_stop", "symbol": symbol,
                                "trade": closed, "setup": setup, "price": current_price,
                            })
                        del self.positions[symbol]

        return updates

    # ──────────────────────────────────────────────────────────────────────────
    def _update_trailing(self, pos: ActivePosition, current_price: float):
        """
        Обновление трейлинг-наклонной по РЕАЛЬНЫМ локальным экстремумам.
        ИСПРАВЛЕНО: не добавляет каждую тиковую цену — только подтверждённые пивоты.

        Для ЛОНГ: ищем локальные минимумы (prev_prev < prev > prev → нет;
                                            prev_prev > prev < current → локальный минимум prev)
        Для ШОРТ: ищем локальные максимумы.
        """
        prev_prev = pos._prev_prev_price
        prev      = pos._prev_price

        if prev_prev > 0 and prev > 0:
            if pos.trade.direction == "LONG":
                # Локальный минимум: prev ниже соседей
                if prev_prev > prev < current_price:
                    # Проверяем, что это значимый новый экстремум (> step от последнего)
                    if not pos.trailing_pivots or (
                        abs(prev - pos.trailing_pivots[-1][1]) / pos.trailing_pivots[-1][1] * 100
                        >= _TRAILING_PIVOT_STEP_PCT
                    ):
                        idx = len(pos.trailing_pivots)
                        pos.trailing_pivots.append((idx, prev))
                        if len(pos.trailing_pivots) > _TRAILING_MAX_POINTS:
                            pos.trailing_pivots = pos.trailing_pivots[-_TRAILING_MAX_POINTS:]
            else:
                # Локальный максимум: prev выше соседей
                if prev_prev < prev > current_price:
                    if not pos.trailing_pivots or (
                        abs(prev - pos.trailing_pivots[-1][1]) / pos.trailing_pivots[-1][1] * 100
                        >= _TRAILING_PIVOT_STEP_PCT
                    ):
                        idx = len(pos.trailing_pivots)
                        pos.trailing_pivots.append((idx, prev))
                        if len(pos.trailing_pivots) > _TRAILING_MAX_POINTS:
                            pos.trailing_pivots = pos.trailing_pivots[-_TRAILING_MAX_POINTS:]

        # Обновляем историю
        pos._prev_prev_price = pos._prev_price
        pos._prev_price      = current_price

        # Строим трендлайн если достаточно точек
        if len(pos.trailing_pivots) >= 2:
            p1 = pos.trailing_pivots[-2]
            p2 = pos.trailing_pivots[-1]

            # Для лонга: линия должна быть восходящей (минимумы растут)
            # Для шорта: линия должна быть нисходящей (максимумы падают)
            valid_slope = (
                p2[1] >= p1[1] if pos.trade.direction == "LONG"
                else p2[1] <= p1[1]
            )
            if valid_slope:
                line_type = "support" if pos.trade.direction == "LONG" else "resistance"
                pos.trailing_trendline = TrendLine(
                    point1_index=p1[0],
                    point1_price=p1[1],
                    point2_index=p2[0],
                    point2_price=p2[1],
                    line_type=line_type,
                )

    # ──────────────────────────────────────────────────────────────────────────
    def get_active_positions_summary(self) -> str:
        if not self.positions:
            return "Нет активных позиций"

        lines = ["📊 Активные позиции:\n"]
        for symbol, pos in self.positions.items():
            trade  = pos.trade
            status = "🟢 ТП1 достигнут" if trade.tp1_hit else "⏳ Ожидание ТП1"
            trailing_info = ""
            if pos.trailing_trendline and len(pos.trailing_pivots) >= 2:
                n          = len(pos.trailing_pivots)
                line_price = pos.trailing_trendline.price_at_index(n - 1)
                trailing_info = f" | trailing: ${format_price(line_price)}"
            lines.append(
                f"  • {symbol} {trade.direction} @ {format_price(trade.entry_price)} "
                f"| SL: {format_price(trade.stop_loss)} | {status}{trailing_info}"
            )

        return "\n".join(lines)
