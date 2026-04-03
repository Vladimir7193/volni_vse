"""
risk_manager.py — мани-менеджмент и управление рисками.

Исправления:
  - remaining_risk не растёт после прибыльных сделок (был баг: += daily_pnl)
  - P&L on deposit умножается на LEVERAGE (фьючерсы!)
  - Логика can_open_trade учитывает MAX_DAILY_LOSS_PCT корректно
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from typing import Optional
from loguru import logger
from config import (
    MAX_DAILY_LOSS_PCT, MAX_DAILY_TRADES, RISK_PER_TRADE_PCT,
    MAX_RE_ENTRIES, TP1_CLOSE_PCT, TP2_CLOSE_PCT, LEVERAGE,
)


@dataclass
class TradeRecord:
    symbol:            str
    direction:         str
    entry_price:       float
    stop_loss:         float
    tp1_price:         float
    tp2_price:         float
    position_size_pct: float
    entry_time:        datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    exit_price:        float = 0.0
    exit_time:         Optional[datetime] = None
    pnl_pct:           float = 0.0          # P&L позиции, %
    pnl_deposit_pct:   float = 0.0          # P&L от депозита с учётом плеча, %
    status:            str = "active"       # active, tp1_hit, tp2_hit, stopped, cancelled
    tp1_hit:           bool = False
    re_entry_count:    int = 0
    setup_quality:     str = "B"
    timeframe:         str = "15"
    setup_type:        str = "setup_1"


class RiskManager:

    def __init__(self):
        self.daily_trades:  list[TradeRecord]       = []
        self.active_trades: dict[str, TradeRecord]  = {}
        self.all_trades:    list[TradeRecord]       = []
        self.current_date:  date                    = datetime.now(timezone.utc).date()
        self.daily_pnl:     float                   = 0.0  # P&L от депозита за день

    # ──────────────────────────────────────────────────────────────────────────
    def _check_new_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self.current_date:
            self.current_date = today
            self.daily_trades = []
            self.daily_pnl    = 0.0
            logger.info(f"Новый торговый день: {today}")

    # ──────────────────────────────────────────────────────────────────────────
    def can_open_trade(self, symbol: str) -> tuple[bool, str]:
        self._check_new_day()

        # Дневной лимит убытка: daily_pnl отрицательный при потерях
        if self.daily_pnl <= -MAX_DAILY_LOSS_PCT:
            return False, f"Дневной лимит убытка исчерпан ({self.daily_pnl:.2f}%)"

        today_trades = [t for t in self.daily_trades if t.status != "cancelled"]
        if len(today_trades) >= MAX_DAILY_TRADES:
            return False, f"Лимит сделок в день ({MAX_DAILY_TRADES})"

        if symbol in self.active_trades:
            return False, f"Уже есть позиция по {symbol}"

        return True, "OK"

    # ──────────────────────────────────────────────────────────────────────────
    def can_re_enter(self, symbol: str, setup_id: str) -> tuple[bool, str]:
        re_entries = sum(
            1 for t in self.daily_trades
            if t.symbol == symbol and t.status == "stopped"
        )
        if re_entries >= MAX_RE_ENTRIES:
            return False, f"Лимит перезаходов ({MAX_RE_ENTRIES})"
        return self.can_open_trade(symbol)

    # ──────────────────────────────────────────────────────────────────────────
    def calculate_position_size(self, stop_loss_pct: float) -> float:
        """
        Размер позиции (% от депозита).
        ИСПРАВЛЕНО: remaining_risk учитывает только убытки (не прибавляет профит).
        """
        if stop_loss_pct <= 0:
            return 0.0

        # Остаток дневного риск-бюджета: только убытки уменьшают лимит
        remaining_risk = MAX_DAILY_LOSS_PCT + min(0.0, self.daily_pnl)
        risk_per_trade = min(RISK_PER_TRADE_PCT, remaining_risk)

        if risk_per_trade <= 0:
            return 0.0

        # Размер с учётом плеча: если плечо 5x и риск 0.5%, то SL distance
        # вычитается из margin (position_size_pct). Формула без плеча:
        # size_notional = risk / sl_pct → size_margin = size_notional / leverage
        size_notional = (risk_per_trade / stop_loss_pct) * 100
        size_margin   = size_notional / LEVERAGE
        return min(size_margin, 100.0)

    # ──────────────────────────────────────────────────────────────────────────
    def open_trade(self, trade: TradeRecord) -> bool:
        can_open, reason = self.can_open_trade(trade.symbol)
        if not can_open:
            logger.warning(f"Не могу открыть {trade.symbol}: {reason}")
            return False

        self.active_trades[trade.symbol] = trade
        self.daily_trades.append(trade)
        self.all_trades.append(trade)
        logger.info(
            f"Открыта сделка: {trade.symbol} {trade.direction} @ {trade.entry_price} "
            f"| SL={trade.stop_loss} | плечо={LEVERAGE}x"
        )
        return True

    # ──────────────────────────────────────────────────────────────────────────
    def close_trade(
        self,
        symbol:     str,
        exit_price: float,
        reason:     str = "manual",
    ) -> Optional[TradeRecord]:
        if symbol not in self.active_trades:
            return None

        trade            = self.active_trades[symbol]
        trade.exit_price = exit_price
        trade.exit_time  = datetime.now(timezone.utc)

        # P&L позиции (без плеча)
        if trade.direction == "LONG":
            trade.pnl_pct = ((exit_price - trade.entry_price) / trade.entry_price) * 100
        else:
            trade.pnl_pct = ((trade.entry_price - exit_price) / trade.entry_price) * 100

        # P&L от депозита С УЧЁТОМ ПЛЕЧА
        # pnl_deposit = pnl_on_position * margin_fraction * leverage
        trade.pnl_deposit_pct = (
            trade.pnl_pct
            * (trade.position_size_pct / 100)
            * LEVERAGE
        )
        trade.status = reason

        self.daily_pnl += trade.pnl_deposit_pct
        del self.active_trades[symbol]

        logger.info(
            f"Закрыта сделка: {symbol} {reason} @ {exit_price} "
            f"P&L: {trade.pnl_pct:+.2f}% (депозит: {trade.pnl_deposit_pct:+.2f}%)"
        )
        return trade

    # ──────────────────────────────────────────────────────────────────────────
    def update_tp1_hit(self, symbol: str, tp1_price: float) -> Optional[TradeRecord]:
        if symbol not in self.active_trades:
            return None

        trade          = self.active_trades[symbol]
        trade.tp1_hit  = True

        # P&L с частичного закрытия (TP1_CLOSE_PCT%)
        if trade.direction == "LONG":
            partial_pnl_pct = ((tp1_price - trade.entry_price) / trade.entry_price) * 100
        else:
            partial_pnl_pct = ((trade.entry_price - tp1_price) / trade.entry_price) * 100

        partial_deposit_pnl = (
            partial_pnl_pct
            * (trade.position_size_pct / 100)
            * LEVERAGE
            * (TP1_CLOSE_PCT / 100)
        )
        self.daily_pnl += partial_deposit_pnl

        # Стоп в безубыток
        trade.stop_loss         = trade.entry_price
        trade.status            = "tp1_hit"
        trade.position_size_pct *= (TP2_CLOSE_PCT / 100)  # остаток 30%

        logger.info(
            f"ТП1 достигнут: {symbol} @ {tp1_price} "
            f"Закрыто {TP1_CLOSE_PCT}%, стоп → безубыток | "
            f"deposit PnL: {partial_deposit_pnl:+.2f}%"
        )
        return trade

    # ──────────────────────────────────────────────────────────────────────────
    def get_daily_stats(self) -> dict:
        self._check_new_day()

        completed = [t for t in self.daily_trades if t.status not in ("active", "tp1_hit")]
        profitable = [t for t in completed if t.pnl_pct > 0]
        losing     = [t for t in completed if t.pnl_pct < 0]
        active     = [t for t in self.daily_trades if t.status in ("active", "tp1_hit")]
        cancelled  = [t for t in self.daily_trades if t.status == "cancelled"]

        best_trade  = max(completed, key=lambda t: t.pnl_pct) if completed else None
        worst_trade = min(completed, key=lambda t: t.pnl_pct) if completed else None

        total_rr = []
        for t in completed:
            risk = abs(t.entry_price - t.stop_loss)
            if risk > 0:
                reward = abs(t.exit_price - t.entry_price)
                total_rr.append(reward / risk)

        return {
            "date":          self.current_date.isoformat(),
            "total_signals": len(self.daily_trades),
            "profitable":    len(profitable),
            "losing":        len(losing),
            "active":        len(active),
            "cancelled":     len(cancelled),
            "daily_pnl":     self.daily_pnl,
            "best_trade":    best_trade,
            "worst_trade":   worst_trade,
            "avg_rr":        sum(total_rr) / len(total_rr) if total_rr else 0,
            "winrate":       (len(profitable) / len(completed) * 100) if completed else 0,
        }

    # ──────────────────────────────────────────────────────────────────────────
    def get_rolling_winrate(self, days: int = 30) -> float:
        completed = [
            t for t in self.all_trades
            if t.status not in ("active", "tp1_hit", "cancelled")
        ]
        if not completed:
            return 0.0

        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
        recent = [t for t in completed if t.entry_time.timestamp() > cutoff]

        if not recent:
            return 0.0

        profitable = [t for t in recent if t.pnl_pct > 0]
        return (len(profitable) / len(recent)) * 100
