"""
position_tracker.py — production-oriented position state tracker.

Совместимость:
- scanner.py вызывает add_position(setup, trade)
- scanner.py вызывает check_positions(prices) и затем отправляет updates в Telegram
- risk_manager.py является источником truth для realised PnL

Задачи:
- хранить runtime-state позиции
- обнаруживать TP1 / TP2 / Stop / BE-stop события
- дергать RiskManager.update_tp1_hit / close_trade
- возвращать human-readable update events для Telegram
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Any

from loguru import logger

from risk_manager import RiskManager, TradeRecord


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class PositionState:
    symbol: str
    direction: str
    timeframe: str
    setup_type: str
    quality: str

    entry_price: float
    stop_loss: float
    tp1_price: float
    tp2_price: float

    opened_at: datetime = field(default_factory=_utc_now)
    last_price: float = 0.0
    last_update_at: datetime = field(default_factory=_utc_now)

    is_open: bool = True
    tp1_hit: bool = False
    tp2_hit: bool = False
    stop_moved_to_breakeven: bool = False

    breakeven_price: float = 0.0
    close_reason: str = ""
    max_favorable_excursion_pct: float = 0.0
    max_adverse_excursion_pct: float = 0.0


class PositionTracker:
    def __init__(self, risk_manager: RiskManager) -> None:
        self.risk_manager = risk_manager
        self.positions: dict[str, PositionState] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_position(self, setup, trade: TradeRecord) -> None:
        state = PositionState(
            symbol=trade.symbol,
            direction=trade.direction,
            timeframe=trade.timeframe,
            setup_type=trade.setup_type,
            quality=trade.setup_quality,
            entry_price=float(trade.entry_price),
            stop_loss=float(trade.stop_loss),
            tp1_price=float(trade.tp1_price),
            tp2_price=float(trade.tp2_price),
            breakeven_price=float(trade.entry_price),
            last_price=float(trade.entry_price),
        )
        self.positions[trade.symbol] = state

        logger.info(
            "Position added: {} {} tf={} type={} quality={}",
            trade.symbol,
            trade.direction,
            trade.timeframe,
            trade.setup_type,
            trade.setup_quality,
        )

    def remove_position(self, symbol: str) -> None:
        self.positions.pop(symbol, None)

    def check_positions(self, prices: dict[str, float]) -> list[dict]:
        updates: list[dict] = []

        for symbol, state in list(self.positions.items()):
            price = _safe_float(prices.get(symbol))
            if price <= 0:
                continue

            state.last_price = price
            state.last_update_at = _utc_now()

            self._update_excursions(state, price)

            trade = self.risk_manager.get_trade(symbol)
            if trade is None:
                logger.warning(
                    "Position {} есть в tracker, но отсутствует в RiskManager. Удаляю из tracker.",
                    symbol,
                )
                self.remove_position(symbol)
                continue

            self.risk_manager.update_unrealized_pnl(symbol, price)

            event = self._process_single_position(state, trade, price)
            if event:
                updates.append(event)

        return updates

    # ------------------------------------------------------------------
    # Core state machine
    # ------------------------------------------------------------------

    def _process_single_position(
        self,
        state: PositionState,
        trade: TradeRecord,
        price: float,
    ) -> Optional[dict]:
        if not state.is_open:
            return None

        # 1. Сначала TP2 — если рынок сразу улетел.
        if not state.tp2_hit and self._is_tp2_hit(state, price):
            closed = self.risk_manager.close_trade(
                symbol=state.symbol,
                exit_price=state.tp2_price,
                reason="tp2_hit",
            )
            if closed:
                state.tp2_hit = True
                state.is_open = False
                state.close_reason = "tp2_hit"
                self.remove_position(state.symbol)
                return self._build_close_event(state, closed, event_type="tp2_hit")

        # 2. Потом TP1 partial.
        if not state.tp1_hit and self._is_tp1_hit(state, price):
            hit = self.risk_manager.update_tp1_hit(
                symbol=state.symbol,
                fill_price=state.tp1_price,
            )
            if hit:
                state.tp1_hit = True
                state.stop_moved_to_breakeven = True
                state.stop_loss = state.entry_price

                updated_trade = self.risk_manager.get_trade(state.symbol)
                return self._build_tp1_event(state, updated_trade)

        # 3. Потом стоп / BE-стоп.
        if self._is_stop_hit(state, price):
            reason = "breakeven_stop" if state.stop_moved_to_breakeven else "stop_loss"
            exit_price = state.stop_loss

            closed = self.risk_manager.close_trade(
                symbol=state.symbol,
                exit_price=exit_price,
                reason=reason,
            )
            if closed:
                state.is_open = False
                state.close_reason = reason
                self.remove_position(state.symbol)
                return self._build_close_event(state, closed, event_type=reason)

        return None

    # ------------------------------------------------------------------
    # Price condition helpers
    # ------------------------------------------------------------------

    def _is_tp1_hit(self, state: PositionState, price: float) -> bool:
        if state.direction == "long":
            return price >= state.tp1_price
        return price <= state.tp1_price

    def _is_tp2_hit(self, state: PositionState, price: float) -> bool:
        if state.direction == "long":
            return price >= state.tp2_price
        return price <= state.tp2_price

    def _is_stop_hit(self, state: PositionState, price: float) -> bool:
        if state.direction == "long":
            return price <= state.stop_loss
        return price >= state.stop_loss

    # ------------------------------------------------------------------
    # Excursions
    # ------------------------------------------------------------------

    def _update_excursions(self, state: PositionState, price: float) -> None:
        mfe = self._signed_move_pct(state.direction, state.entry_price, price)
        mae = self._signed_move_pct(self._opposite(state.direction), state.entry_price, price)

        if mfe > state.max_favorable_excursion_pct:
            state.max_favorable_excursion_pct = round(mfe, 6)

        if mae > state.max_adverse_excursion_pct:
            state.max_adverse_excursion_pct = round(mae, 6)

    def _signed_move_pct(self, direction: str, entry: float, current: float) -> float:
        if entry <= 0 or current <= 0:
            return 0.0

        if direction == "long":
            return max(0.0, ((current - entry) / entry) * 100.0)
        return max(0.0, ((entry - current) / entry) * 100.0)

    def _opposite(self, direction: str) -> str:
        return "short" if direction == "long" else "long"

    # ------------------------------------------------------------------
    # Telegram / update payloads
    # ------------------------------------------------------------------

    def _build_tp1_event(self, state: PositionState, trade: Optional[TradeRecord]) -> dict:
        realized = float(trade.realized_pnl_pct) if trade else 0.0
        remaining = float(trade.remaining_position_size_pct) if trade else 0.0

        return {
            "event": "tp1_hit",
            "symbol": state.symbol,
            "direction": state.direction,
            "timeframe": state.timeframe,
            "setup_type": state.setup_type,
            "quality": state.quality,
            "price": state.tp1_price,
            "entry_price": state.entry_price,
            "stop_loss": state.stop_loss,
            "tp1_price": state.tp1_price,
            "tp2_price": state.tp2_price,
            "realized_pnl_pct": realized,
            "remaining_position_size_pct": remaining,
            "breakeven_activated": True,
            "mfe_pct": state.max_favorable_excursion_pct,
            "mae_pct": state.max_adverse_excursion_pct,
            "timestamp": _utc_now().isoformat(),
            "message": (
                f"{state.symbol} TP1 hit. Partial profit booked, "
                f"stop moved to breakeven, remaining size={remaining:.3f}%."
            ),
        }

    def _build_close_event(
        self,
        state: PositionState,
        closed_trade: TradeRecord,
        event_type: str,
    ) -> dict:
        return {
            "event": event_type,
            "symbol": state.symbol,
            "direction": state.direction,
            "timeframe": state.timeframe,
            "setup_type": state.setup_type,
            "quality": state.quality,
            "entry_price": state.entry_price,
            "exit_price": closed_trade.exit_price,
            "stop_loss": state.stop_loss,
            "tp1_price": state.tp1_price,
            "tp2_price": state.tp2_price,
            "realized_pnl_pct": float(closed_trade.realized_pnl_pct),
            "total_pnl_pct": float(closed_trade.total_pnl_pct),
            "close_reason": closed_trade.close_reason,
            "tp1_hit_before_close": state.tp1_hit,
            "mfe_pct": state.max_favorable_excursion_pct,
            "mae_pct": state.max_adverse_excursion_pct,
            "opened_at": state.opened_at.isoformat(),
            "closed_at": closed_trade.closed_at.isoformat() if closed_trade.closed_at else None,
            "timestamp": _utc_now().isoformat(),
            "message": (
                f"{state.symbol} closed by {closed_trade.close_reason}. "
                f"Final realized PnL={closed_trade.realized_pnl_pct:.4f}%."
            ),
        }