"""
risk_manager.py — production-oriented risk and trade accounting.

Цели:
- единый контроль дневного риска
- корректный учёт realised PnL при partial TP
- совместимость с существующим scanner / position_tracker
- ясная модель открытых и закрытых сделок
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from typing import Optional

from loguru import logger

from config import (
    DAILY_RISK_LIMIT_PCT,
    MAX_TRADES_PER_DAY,
    MAX_CONCURRENT_TRADES,
    RISK_PER_TRADE_PCT,
    LEVERAGE,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_pct(value: float) -> float:
    return round(float(value), 6)


@dataclass(slots=True)
class TradeRecord:
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    tp1_price: float
    tp2_price: float
    position_size_pct: float
    setup_quality: str
    timeframe: str
    setup_type: str

    opened_at: datetime = field(default_factory=_utc_now)
    closed_at: Optional[datetime] = None

    status: str = "open"
    exit_price: Optional[float] = None
    close_reason: Optional[str] = None

    leverage: float = LEVERAGE

    initial_position_size_pct: float = 0.0
    remaining_position_size_pct: float = 0.0

    tp1_hit: bool = False
    tp1_hit_at: Optional[datetime] = None

    realized_pnl_pct: float = 0.0
    unrealized_pnl_pct: float = 0.0
    total_pnl_pct: float = 0.0

    realized_r_multiple: float = 0.0
    fees_pct: float = 0.0
    funding_pct: float = 0.0

    def __post_init__(self) -> None:
        self.position_size_pct = float(self.position_size_pct)
        self.initial_position_size_pct = float(self.position_size_pct)
        self.remaining_position_size_pct = float(self.position_size_pct)

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def is_closed(self) -> bool:
        return self.status == "closed"

    @property
    def risk_distance_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return abs(self.entry_price - self.stop_loss) / self.entry_price * 100.0

    @property
    def side_sign(self) -> int:
        return 1 if self.direction.lower() == "long" else -1


class RiskManager:
    def __init__(self) -> None:
        self.active_trades: dict[str, TradeRecord] = {}
        self.closed_trades: list[TradeRecord] = []

        self.current_day: date = _utc_now().date()
        self.daily_realized_pnl_pct: float = 0.0
        self.daily_realized_loss_pct: float = 0.0
        self.daily_trade_count: int = 0

    # -------------------------------------------------------------------------
    # Day state
    # -------------------------------------------------------------------------

    def _roll_day_if_needed(self) -> None:
        today = _utc_now().date()
        if today != self.current_day:
            logger.info(
                "Новый торговый день: {} -> {}. Сброс дневных счётчиков.",
                self.current_day,
                today,
            )
            self.current_day = today
            self.daily_realized_pnl_pct = 0.0
            self.daily_realized_loss_pct = 0.0
            self.daily_trade_count = 0

    # -------------------------------------------------------------------------
    # Risk helpers
    # -------------------------------------------------------------------------

    def get_available_daily_risk_pct(self) -> float:
        self._roll_day_if_needed()
        remaining = DAILY_RISK_LIMIT_PCT - self.daily_realized_loss_pct
        return max(0.0, _safe_pct(remaining))

    def get_open_risk_pct(self) -> float:
        open_risk = 0.0
        for trade in self.active_trades.values():
            open_risk += min(trade.remaining_position_size_pct, RISK_PER_TRADE_PCT)
        return _safe_pct(open_risk)

    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss: float,
        setup_quality: str = "B",
    ) -> float:
        self._roll_day_if_needed()

        if entry_price <= 0 or stop_loss <= 0:
            return 0.0

        stop_distance_pct = abs(entry_price - stop_loss) / entry_price * 100.0
        if stop_distance_pct <= 0:
            return 0.0

        quality_multiplier = {
            "A+": 1.00,
            "A": 0.90,
            "B": 0.75,
            "C": 0.50,
        }.get(setup_quality, 0.60)

        available_daily_risk = self.get_available_daily_risk_pct()
        per_trade_risk_cap = min(RISK_PER_TRADE_PCT, available_daily_risk)
        if per_trade_risk_cap <= 0:
            return 0.0

        effective_risk_pct = per_trade_risk_cap * quality_multiplier

        raw_position_size_pct = effective_risk_pct / stop_distance_pct * 100.0

        max_position_size_pct = {
            "A+": 25.0,
            "A": 20.0,
            "B": 14.0,
            "C": 8.0,
        }.get(setup_quality, 10.0)

        position_size_pct = min(raw_position_size_pct, max_position_size_pct)
        return _safe_pct(max(0.0, position_size_pct))

    def can_open_trade(self, symbol: str) -> tuple[bool, str]:
        self._roll_day_if_needed()

        if symbol in self.active_trades:
            return False, f"Уже есть активная позиция по {symbol}"

        if len(self.active_trades) >= MAX_CONCURRENT_TRADES:
            return False, f"Достигнут лимит одновременных сделок: {MAX_CONCURRENT_TRADES}"

        if self.daily_trade_count >= MAX_TRADES_PER_DAY:
            return False, f"Достигнут дневной лимит сделок: {MAX_TRADES_PER_DAY}"

        if self.get_available_daily_risk_pct() <= 0:
            return False, "Дневной лимит риска исчерпан"

        return True, "OK"

    # -------------------------------------------------------------------------
    # Trade lifecycle
    # -------------------------------------------------------------------------

    def open_trade(self, trade: TradeRecord) -> bool:
        can_open, reason = self.can_open_trade(trade.symbol)
        if not can_open:
            logger.warning("open_trade rejected for {}: {}", trade.symbol, reason)
            return False

        if trade.position_size_pct <= 0:
            logger.warning("open_trade rejected for {}: position_size_pct <= 0", trade.symbol)
            return False

        trade.status = "open"
        trade.opened_at = _utc_now()
        trade.initial_position_size_pct = float(trade.position_size_pct)
        trade.remaining_position_size_pct = float(trade.position_size_pct)
        trade.realized_pnl_pct = 0.0
        trade.unrealized_pnl_pct = 0.0
        trade.total_pnl_pct = 0.0

        self.active_trades[trade.symbol] = trade
        self.daily_trade_count += 1

        logger.success(
            "Открыта сделка {} {} size={:.3f}% quality={} tf={} type={}",
            trade.symbol,
            trade.direction,
            trade.position_size_pct,
            trade.setup_quality,
            trade.timeframe,
            trade.setup_type,
        )
        return True

    def update_unrealized_pnl(self, symbol: str, mark_price: float) -> float:
        trade = self.active_trades.get(symbol)
        if not trade or trade.remaining_position_size_pct <= 0 or mark_price <= 0:
            return 0.0

        pnl_pct = self._calculate_position_pnl_pct(
            direction=trade.direction,
            entry_price=trade.entry_price,
            exit_price=mark_price,
            position_size_pct=trade.remaining_position_size_pct,
        )
        trade.unrealized_pnl_pct = pnl_pct
        trade.total_pnl_pct = _safe_pct(trade.realized_pnl_pct + trade.unrealized_pnl_pct)
        return trade.unrealized_pnl_pct

    def update_tp1_hit(self, symbol: str, fill_price: Optional[float] = None) -> bool:
        trade = self.active_trades.get(symbol)
        if not trade or trade.tp1_hit:
            return False

        fill = float(fill_price) if fill_price else float(trade.tp1_price)
        if fill <= 0:
            return False

        closed_fraction = 0.50
        closed_size = trade.remaining_position_size_pct * closed_fraction
        if closed_size <= 0:
            return False

        realized_chunk = self._calculate_position_pnl_pct(
            direction=trade.direction,
            entry_price=trade.entry_price,
            exit_price=fill,
            position_size_pct=closed_size,
        )

        trade.realized_pnl_pct = _safe_pct(trade.realized_pnl_pct + realized_chunk)
        trade.remaining_position_size_pct = _safe_pct(trade.remaining_position_size_pct - closed_size)
        trade.tp1_hit = True
        trade.tp1_hit_at = _utc_now()

        # Переносим стоп в безубыток после TP1.
        trade.stop_loss = trade.entry_price
        trade.total_pnl_pct = _safe_pct(trade.realized_pnl_pct + trade.unrealized_pnl_pct)

        self._apply_realized_pnl(realized_chunk)

        logger.info(
            "TP1 hit: {} realized={:.4f}% remaining={:.4f}%",
            symbol,
            realized_chunk,
            trade.remaining_position_size_pct,
        )
        return True

    def close_trade(
        self,
        symbol: str,
        exit_price: float,
        reason: str = "manual_close",
    ) -> Optional[TradeRecord]:
        trade = self.active_trades.get(symbol)
        if not trade:
            logger.warning("close_trade: активная сделка по {} не найдена", symbol)
            return None

        if exit_price <= 0:
            logger.warning("close_trade: некорректная цена выхода по {}", symbol)
            return None

        final_realized = 0.0
        if trade.remaining_position_size_pct > 0:
            final_realized = self._calculate_position_pnl_pct(
                direction=trade.direction,
                entry_price=trade.entry_price,
                exit_price=exit_price,
                position_size_pct=trade.remaining_position_size_pct,
            )

        trade.realized_pnl_pct = _safe_pct(trade.realized_pnl_pct + final_realized)
        trade.unrealized_pnl_pct = 0.0
        trade.total_pnl_pct = trade.realized_pnl_pct

        trade.exit_price = float(exit_price)
        trade.close_reason = reason
        trade.closed_at = _utc_now()
        trade.status = "closed"
        trade.remaining_position_size_pct = 0.0

        self._apply_realized_pnl(final_realized)

        self.closed_trades.append(trade)
        del self.active_trades[symbol]

        logger.info(
            "Сделка закрыта {} reason={} total_realized={:.4f}%",
            symbol,
            reason,
            trade.realized_pnl_pct,
        )
        return trade

    # -------------------------------------------------------------------------
    # Analytics
    # -------------------------------------------------------------------------

    def _apply_realized_pnl(self, pnl_pct: float) -> None:
        pnl_pct = float(pnl_pct)
        self.daily_realized_pnl_pct = _safe_pct(self.daily_realized_pnl_pct + pnl_pct)
        if pnl_pct < 0:
            self.daily_realized_loss_pct = _safe_pct(self.daily_realized_loss_pct + abs(pnl_pct))

    def _calculate_position_pnl_pct(
        self,
        direction: str,
        entry_price: float,
        exit_price: float,
        position_size_pct: float,
    ) -> float:
        if entry_price <= 0 or exit_price <= 0 or position_size_pct <= 0:
            return 0.0

        if direction.lower() == "long":
            price_move_pct = ((exit_price - entry_price) / entry_price) * 100.0
        else:
            price_move_pct = ((entry_price - exit_price) / entry_price) * 100.0

        leveraged_pnl_pct = price_move_pct * (position_size_pct / 100.0) * LEVERAGE
        return _safe_pct(leveraged_pnl_pct)

    def get_daily_stats(self) -> dict:
        self._roll_day_if_needed()

        wins = sum(1 for t in self.closed_trades if t.closed_at and t.closed_at.date() == self.current_day and t.total_pnl_pct > 0)
        losses = sum(1 for t in self.closed_trades if t.closed_at and t.closed_at.date() == self.current_day and t.total_pnl_pct <= 0)

        return {
            "date": str(self.current_day),
            "daily_realized_pnl_pct": _safe_pct(self.daily_realized_pnl_pct),
            "daily_realized_loss_pct": _safe_pct(self.daily_realized_loss_pct),
            "available_daily_risk_pct": self.get_available_daily_risk_pct(),
            "daily_trade_count": self.daily_trade_count,
            "active_trades": len(self.active_trades),
            "closed_trades_today": wins + losses,
            "wins_today": wins,
            "losses_today": losses,
        }

    def get_rolling_winrate(self, last_n: int = 30) -> float:
        if last_n <= 0:
            return 0.0

        closed = self.closed_trades[-last_n:]
        if not closed:
            return 0.0

        wins = sum(1 for trade in closed if trade.total_pnl_pct > 0)
        return _safe_pct((wins / len(closed)) * 100.0)

    def get_trade(self, symbol: str) -> Optional[TradeRecord]:
        return self.active_trades.get(symbol)

    def get_all_active_trades(self) -> list[TradeRecord]:
        return list(self.active_trades.values())

    def get_all_closed_trades(self) -> list[TradeRecord]:
        return list(self.closed_trades)
