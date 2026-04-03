"""
Telegram бот для отправки сигналов и обновлений.
"""

import asyncio
from typing import Optional
from loguru import logger

import httpx
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY
)
from setups import TradeSetup
from risk_manager import TradeRecord
from utils import format_price, format_pct


class TelegramSender:
    """Отправка сообщений в Telegram"""

    def __init__(self):
        self.bot_token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.proxy = TELEGRAM_PROXY
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.max_retries = 3
        self.retry_delay = 2

    def _get_client(self) -> httpx.AsyncClient:
        """Создание HTTP клиента с прокси"""
        if self.proxy:
            return httpx.AsyncClient(proxy=self.proxy, timeout=30.0)
        return httpx.AsyncClient(timeout=30.0)

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Отправка сообщения в Telegram"""
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }

        for attempt in range(self.max_retries):
            try:
                async with self._get_client() as client:
                    response = await client.post(url, json=payload)
                    if response.status_code == 200:
                        logger.debug("Сообщение отправлено в Telegram")
                        return True
                    elif response.status_code == 429:
                        # Rate limit
                        retry_after = response.json().get('parameters', {}).get('retry_after', 5)
                        logger.warning(f"Telegram rate limit, ждём {retry_after}с")
                        await asyncio.sleep(retry_after)
                    else:
                        logger.error(f"Telegram API error: {response.status_code} {response.text}")
            except Exception as e:
                logger.error(f"Ошибка отправки в Telegram (попытка {attempt+1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay)

        return False

    async def send_long_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Отправка длинного сообщения (разбивка на части)"""
        max_len = 4000
        if len(text) <= max_len:
            return await self.send_message(text, parse_mode)

        parts = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > max_len:
                parts.append(current)
                current = line
            else:
                current += "\n" + line if current else line
        if current:
            parts.append(current)

        success = True
        for part in parts:
            if not await self.send_message(part, parse_mode):
                success = False
            await asyncio.sleep(0.5)

        return success

    # ═══════════════════════════════════════════
    # ФОРМАТИРОВАНИЕ СИГНАЛОВ
    # ═══════════════════════════════════════════

    def format_full_signal(self, setup: TradeSetup) -> str:
        """Форматирование полного торгового сигнала"""
        emoji_dir = "🟢" if setup.direction == "LONG" else "🔴"

        # Фибоначчи уровни
        fib_text = ""
        if setup.fib_levels:
            fib_lines = []
            for level, price in sorted(setup.fib_levels.items()):
                fib_lines.append(f"   • {level:.1%} = ${format_price(price)}")
            fib_text = "\n".join(fib_lines)

        # Качество
        quality_stars = {
            "A+": "⭐⭐⭐⭐⭐",
            "A": "⭐⭐⭐⭐",
            "B": "⭐⭐⭐",
            "C": "⭐⭐"
        }

        msg = f"""
{emoji_dir} <b>СИГНАЛ: {setup.direction}</b>
📊 Монета: <b>{setup.symbol}</b>
⏰ Таймфрейм: {setup.timeframe}m
📋 Сетап: {setup.setup_name}

═══ ВОЛНОВАЯ СТРУКТУРА ═══
🌊 <b>Текущий подсчёт:</b>
{setup.wave_description}

📐 Правило чередования: {setup.alternation_description}

🔍 <b>Контекст старшего ТФ:</b>
{setup.senior_context if setup.senior_context else 'Не определён'}

═══ ПАРАМЕТРЫ ВХОДА ═══
▶️ Точка входа: <b>${format_price(setup.entry_price)}</b> {'(пробой наклонной)' if setup.trendline_broken else '(ожидание пробоя)'}
🛑 Стоп-лосс: <b>${format_price(setup.stop_loss)}</b> ({format_pct(-setup.stop_loss_pct)})
   Основание: {setup.stop_reason}

🎯 ТП1 (70%): <b>${format_price(setup.tp1_price)}</b> ({format_pct(setup.tp1_pct)}) — {setup.tp1_fib_level}
🎯 ТП2 (30%): <b>${format_price(setup.tp2_price)}</b> ({format_pct(setup.tp2_pct)}) — {setup.tp2_fib_level}

📊 R:R до ТП1 = 1:{setup.rr_to_tp1:.1f}
📊 R:R средневзвешенный = 1:{setup.rr_weighted:.1f}
💰 Рекомендуемый объём: <b>{setup.position_size_pct:.0f}%</b> от депозита

═══ ДОПОЛНИТЕЛЬНЫЕ УРОВНИ ═══
📏 Фибоначчи от ${format_price(setup.fib_from)} до ${format_price(setup.fib_to)}:
{fib_text}
"""

        if setup.wave_equality_price > 0:
            msg += f"\n📐 Равенство волн: ${format_price(setup.wave_equality_price)}"
        if setup.wave3_ext_161 > 0:
            msg += f"\n📐 Удлинение 161.8%: ${format_price(setup.wave3_ext_161)}"
        if setup.wave3_ext_227 > 0:
            msg += f"\n📐 Удлинение 227.2%: ${format_price(setup.wave3_ext_227)}"

        msg += f"""

═══ УПРАВЛЕНИЕ ПОЗИЦИЕЙ ═══
1️⃣ При ТП1 → закрыть 70%, стоп → безубыток
2️⃣ Провести растущую наклонную по минимумам
3️⃣ При пробое наклонной → закрыть остаток
4️⃣ При ТП2 → закрыть всё

⚠️ Если стоп сработает:
→ Скорректировать наклонную
→ Ждать новый пробой
→ Макс. 2 перезахода

═══ ОЦЕНКА КАЧЕСТВА ═══
{quality_stars.get(setup.quality, '⭐⭐')} Качество: <b>{setup.quality}</b>

⚠️ <i>Данный сигнал является аналитическим мнением на основе волнового анализа. Это НЕ финансовая рекомендация. Всегда используйте стоп-лосс. Не рискуйте более 1.5% депозита в день.</i>
"""
        return msg.strip()

    def format_forming_signal(self, setup: TradeSetup) -> str:
        """Форматирование предварительного (формирующегося) сигнала"""
        emoji_dir = "🟡"

        line_price = "N/A"
        if setup.trendline:
            # Примерная цена пробоя
            line_price = format_price(setup.entry_price)

        msg = f"""
{emoji_dir} <b>⏳ ФОРМИРУЕТСЯ СЕТАП</b>

📊 Монета: <b>{setup.symbol}</b>
⏰ Таймфрейм: {setup.timeframe}m
📋 Сетап: {setup.setup_name}
📈 Направление: {setup.direction}

🌊 {setup.wave_description}

📐 Наклонная линия: ожидаем пробой на ~${line_price}
⭐ Качество: {setup.quality}

👀 Слежу. Полный сигнал — при пробое.
"""
        return msg.strip()

    def format_update(self, update: dict) -> str:
        """Форматирование обновления позиции"""
        symbol = update['symbol']
        trade = update['trade']
        price = update['price']
        update_type = update['type']

        if update_type == 'tp1_hit':
            pnl = abs(((price - trade.entry_price) / trade.entry_price) * 100)
            msg = f"""
📌 <b>ОБНОВЛЕНИЕ: {symbol} {trade.direction}</b>

✅ <b>ТП1 достигнут</b> — закрыто 70% по ${format_price(price)} ({format_pct(pnl)})
→ Стоп перемещён в безубыток ${format_price(trade.entry_price)}
→ Остаток 30% цель ТП2 ${format_price(update['setup'].tp2_price)}
"""

        elif update_type == 'tp2_hit':
            msg = f"""
📌 <b>ОБНОВЛЕНИЕ: {symbol} {trade.direction}</b>

✅✅ <b>ТП2 достигнут</b> — закрыто 100% по ${format_price(price)}
💰 P&L: {format_pct(trade.pnl_pct)} (депозит: {format_pct(trade.pnl_deposit_pct)})
"""

        elif update_type == 'stop_hit':
            msg = f"""
📌 <b>ОБНОВЛЕНИЕ: {symbol} {trade.direction}</b>

❌ <b>Стоп-лосс сработал</b> по ${format_price(price)}
💔 Убыток: {format_pct(trade.pnl_pct)} (депозит: {format_pct(trade.pnl_deposit_pct)})
→ {'Ожидаем перезаход' if trade.re_entry_count < 2 else 'Сетап отменён'}
"""

        elif update_type == 'trailing_stop':
            msg = f"""
📌 <b>ОБНОВЛЕНИЕ: {symbol} {trade.direction}</b>

📊 <b>Трейлинг-стоп сработал</b> по ${format_price(price)}
💰 P&L: {format_pct(trade.pnl_pct)} (депозит: {format_pct(trade.pnl_deposit_pct)})
"""

        else:
            msg = f"""
📌 <b>ОБНОВЛЕНИЕ: {symbol} {trade.direction}</b>

ℹ️ {update.get('message', 'Обновление')}
"""

        return msg.strip()

    def format_daily_report(self, stats: dict, rolling_winrate: float) -> str:
        """Форматирование дневного отчёта"""
        best = stats.get('best_trade')
        worst = stats.get('worst_trade')

        best_str = f"{best.symbol} {format_pct(best.pnl_pct)}" if best else "—"
        worst_str = f"{worst.symbol} {format_pct(worst.pnl_pct)}" if worst else "—"

        pnl_emoji = "📈" if stats['daily_pnl'] >= 0 else "📉"

        msg = f"""
📊 <b>ДНЕВНОЙ ОТЧЁТ {stats['date']}</b>

Всего сигналов: {stats['total_signals']}
✅ Прибыльных: {stats['profitable']} ({stats['winrate']:.0f}%)
❌ Убыточных: {stats['losing']}
⏳ Активных: {stats['active']}
🚫 Отменённых: {stats['cancelled']}

{pnl_emoji} <b>Общий P&L дня: {format_pct(stats['daily_pnl'])} от депозита</b>
📈 Лучшая сделка: {best_str}
📉 Худшая сделка: {worst_str}
📊 Средний R:R: 1:{stats['avg_rr']:.1f}
📊 Винрейт (30 дней): {rolling_winrate:.0f}%

⚠️ <i>Прошлые результаты не гарантируют будущих.</i>
"""
        return msg.strip()

    async def send_signal(self, setup: TradeSetup):
        """Отправка торгового сигнала"""
        if setup.trendline_broken:
            msg = self.format_full_signal(setup)
        else:
            msg = self.format_forming_signal(setup)

        await self.send_long_message(msg)

    async def send_update(self, update: dict):
        """Отправка обновления позиции"""
        msg = self.format_update(update)
        await self.send_message(msg)

    async def send_daily_report(self, stats: dict, rolling_winrate: float):
        """Отправка дневного отчёта"""
        msg = self.format_daily_report(stats, rolling_winrate)
        await self.send_message(msg)

    async def send_startup_message(self):
        """Сообщение при запуске"""
        msg = """
🤖 <b>BYBIT WAVE SCANNER v1.0</b>

✅ Сканер запущен
📊 Мониторинг: 50 монет
⏰ Таймфреймы: 1m / 5m / 15m / 1H / 4H / 1D
🎯 Сетапы: 5 волн + наклонка | Флаг + пробой
💰 Макс. риск: 1.5% / день

🔄 Сканирование каждые 60 секунд...
"""
        await self.send_message(msg.strip())

    async def send_error(self, error_msg: str):
        """Отправка сообщения об ошибке"""
        msg = f"⚠️ <b>ОШИБКА СКАНЕРА:</b>\n{error_msg}"
        await self.send_message(msg)
