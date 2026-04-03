"""
telegram_bot.py — async Telegram notification sender with optional SOCKS5 proxy.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from aiohttp_socks import ProxyConnector
from loguru import logger

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY


class TelegramSender:
    def __init__(self) -> None:
        self.token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)

        self.proxy_url = TELEGRAM_PROXY
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.enabled else ""
        self._timeout = aiohttp.ClientTimeout(total=15)

    async def send_startup_message(self) -> None:
        if not self.enabled:
            logger.warning("Telegram disabled: startup message skipped")
            return
        await self._send_text("🚀 Bybit Wave Scanner запущен")

    async def send_error(self, message: str) -> None:
        if not self.enabled:
            return
        await self._send_text(f"❌ Ошибка сканера\n\n{message}")

    async def send_signal(self, setup) -> None:
        if not self.enabled:
            return

        status = "CONFIRMED BREAKOUT" if getattr(setup, "trendline_broken", False) else "FORMING"
        text = (
            f"📡 {status}\n"
            f"Symbol: {setup.symbol}\n"
            f"TF: {setup.timeframe}\n"
            f"Type: {setup.setup_type}\n"
            f"Direction: {setup.direction}\n"
            f"Quality: {setup.quality}\n"
            f"Score: {getattr(setup, 'score', 0)}\n"
            f"Entry: {setup.entry_price:.6f}\n"
            f"SL: {setup.stop_loss:.6f}\n"
            f"TP1: {setup.tp1_price:.6f}\n"
            f"TP2: {setup.tp2_price:.6f}\n"
            f"RR: {getattr(setup, 'rr_ratio', 0):.2f}\n"
            f"PosSize: {setup.position_size_pct:.3f}%"
        )
        await self._send_text(text)

    async def send_update(self, update: dict[str, Any]) -> None:
        if not self.enabled:
            return

        event = update.get("event", "update")
        if event == "tp1_hit":
            text = (
                f"✅ TP1 HIT\n"
                f"Symbol: {update.get('symbol')}\n"
                f"Direction: {update.get('direction')}\n"
                f"TF: {update.get('timeframe')}\n"
                f"Realized PnL: {update.get('realized_pnl_pct', 0):.4f}%\n"
                f"Remaining size: {update.get('remaining_position_size_pct', 0):.3f}%\n"
                f"Breakeven activated: {update.get('breakeven_activated')}\n"
                f"MFE: {update.get('mfe_pct', 0):.4f}% | MAE: {update.get('mae_pct', 0):.4f}%"
            )
        else:
            text = (
                f"📕 POSITION CLOSED\n"
                f"Event: {event}\n"
                f"Symbol: {update.get('symbol')}\n"
                f"Reason: {update.get('close_reason')}\n"
                f"Entry: {update.get('entry_price')}\n"
                f"Exit: {update.get('exit_price')}\n"
                f"PnL: {update.get('total_pnl_pct', 0):.4f}%\n"
                f"MFE: {update.get('mfe_pct', 0):.4f}% | MAE: {update.get('mae_pct', 0):.4f}%"
            )

        await self._send_text(text)

    async def send_daily_report(self, stats: dict, rolling_wr: float) -> None:
        if not self.enabled:
            return

        text = (
            f"📊 Daily Report\n"
            f"Date: {stats.get('date')}\n"
            f"Daily Realized PnL: {stats.get('daily_realized_pnl_pct', 0):.4f}%\n"
            f"Daily Realized Loss: {stats.get('daily_realized_loss_pct', 0):.4f}%\n"
            f"Available Daily Risk: {stats.get('available_daily_risk_pct', 0):.4f}%\n"
            f"Trades Today: {stats.get('daily_trade_count', 0)}\n"
            f"Active Trades: {stats.get('active_trades', 0)}\n"
            f"Wins Today: {stats.get('wins_today', 0)} | Losses Today: {stats.get('losses_today', 0)}\n"
            f"Rolling Winrate(30): {rolling_wr:.2f}%"
        )
        await self._send_text(text)

    async def _send_text(self, text: str) -> None:
        if not self.enabled:
            return

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }

        try:
            connector = None
            if self.proxy_url:
                connector = ProxyConnector.from_url(self.proxy_url)

            async with aiohttp.ClientSession(timeout=self._timeout, connector=connector) as session:
                async with session.post(f"{self.base_url}/sendMessage", json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(
                            "Telegram sendMessage failed: status={} body={}",
                            resp.status,
                            body,
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Telegram send failed: {}", exc)