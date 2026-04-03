"""
scanner.py — основной сканер.

Исправления:
  - asyncio.get_running_loop() вместо устаревшего get_event_loop()
  - Telegram-сигнал отправляется ПОСЛЕ успешного open_trade
  - Глобальный кэш 240m данных с TTL (SENIOR_CACHE_TTL_SECONDS)
    снижает количество API-вызовов в ~3 раза
  - credentials удалены из config.py → только os.getenv()
"""
import functools
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional
import pandas as pd
from loguru import logger

from pybit.unified_trading import HTTP

from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, SYMBOLS,
    BYBIT_INTERVALS, KLINE_LIMIT, SCAN_INTERVAL_SECONDS,
    SIGNAL_COOLDOWN_MINUTES, SENIOR_CACHE_TTL_SECONDS,
)
from setups import SetupDetector, TradeSetup
from risk_manager import RiskManager, TradeRecord
from position_tracker import PositionTracker
from telegram_bot import TelegramSender
from utils import RateLimiter, SignalCooldown


class BybitDataFetcher:

    def __init__(self):
        self.client = HTTP(
            api_key    = BYBIT_API_KEY,
            api_secret = BYBIT_API_SECRET,
            testnet    = False,
        )
        self.rate_limiter = RateLimiter(max_calls=10, period=1.0)

    async def get_klines(
        self,
        symbol:   str,
        interval: str,
        limit:    int = 200,
    ) -> Optional[pd.DataFrame]:
        try:
            await self.rate_limiter.acquire()

            bybit_interval = BYBIT_INTERVALS.get(interval, interval)
            actual_limit   = KLINE_LIMIT.get(interval, limit)

            # ИСПРАВЛЕНО: get_running_loop() вместо deprecated get_event_loop()
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                functools.partial(
                    self.client.get_kline,
                    category = "linear",
                    symbol   = symbol,
                    interval = bybit_interval,
                    limit    = actual_limit,
                )
            )

            if response['retCode'] != 0:
                logger.error(f"Bybit API error {symbol} {interval}: {response['retMsg']}")
                return None

            klines = response['result']['list']
            if not klines:
                return None

            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'
            ])
            for col in ['timestamp', 'open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col])

            df = df.sort_values('timestamp').reset_index(drop=True)
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('datetime', inplace=True)
            return df

        except Exception as e:
            logger.error(f"Ошибка загрузки данных {symbol} {interval}: {e}")
            return None

    async def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        prices = {}
        try:
            await self.rate_limiter.acquire()
            symbols_set = set(symbols)

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                functools.partial(self.client.get_tickers, category="linear"),
            )

            if response['retCode'] == 0:
                for ticker in response['result']['list']:
                    sym = ticker['symbol']
                    if sym in symbols_set:
                        prices[sym] = float(ticker['lastPrice'])

        except Exception as e:
            logger.error(f"Ошибка получения цен: {e}")

        return prices

    async def get_current_price(self, symbol: str) -> float:
        prices = await self.get_current_prices([symbol])
        return prices.get(symbol, 0.0)


class Scanner:

    def __init__(self):
        self.data_fetcher    = BybitDataFetcher()
        self.setup_detector  = SetupDetector()
        self.risk_manager    = RiskManager()
        self.position_tracker = PositionTracker(self.risk_manager)
        self.telegram        = TelegramSender()
        self.signal_cooldown = SignalCooldown(SIGNAL_COOLDOWN_MINUTES)
        self.is_running      = False
        self.scan_count      = 0

        # Кэш 240m данных: {symbol: (fetched_at, df)}
        # Обновляется не чаще SENIOR_CACHE_TTL_SECONDS
        self._senior_cache: dict[str, tuple[float, pd.DataFrame]] = {}

    # ──────────────────────────────────────────────────────────────────────────

    async def start(self):
        self.is_running = True
        logger.info("═══ BYBIT WAVE SCANNER ЗАПУЩЕН ═══")
        await self.telegram.send_startup_message()

        tasks = [
            asyncio.create_task(self._scan_loop(),             name="scan"),
            asyncio.create_task(self._position_monitor_loop(), name="monitor"),
            asyncio.create_task(self._daily_report_loop(),     name="report"),
        ]
        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        except Exception as e:
            logger.critical(f"Задача упала: {e}")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self):
        self.is_running = False
        logger.info("═══ BYBIT WAVE SCANNER ОСТАНОВЛЕН ═══")

    # ──────────────────────────────────────────────────────────────────────────
    #  Кэш старшего ТФ
    # ──────────────────────────────────────────────────────────────────────────

    async def _get_senior_df(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        240m данные с TTL-кэшем (SENIOR_CACHE_TTL_SECONDS).
        Запрашивает не чаще чем раз в 15 минут на символ.
        """
        now    = time.monotonic()
        cached = self._senior_cache.get(symbol)

        if cached:
            fetched_at, df = cached
            if now - fetched_at < SENIOR_CACHE_TTL_SECONDS:
                return df

        df = await self.data_fetcher.get_klines(symbol, "240")
        if df is not None:
            self._senior_cache[symbol] = (now, df)
        return df

    # ──────────────────────────────────────────────────────────────────────────
    #  Основной цикл
    # ──────────────────────────────────────────────────────────────────────────

    async def _scan_loop(self):
        while self.is_running:
            try:
                self.scan_count += 1
                logger.info(f"═══ Сканирование #{self.scan_count} ═══")

                prices = await self.data_fetcher.get_current_prices(SYMBOLS)
                if not prices:
                    logger.warning("Не удалось получить цены")
                    await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                    continue

                all_setups = []
                for symbol in SYMBOLS:
                    if symbol not in prices:
                        continue
                    try:
                        setups = await self._scan_symbol(symbol, prices[symbol])
                        all_setups.extend(setups)
                    except Exception as e:
                        logger.error(f"Ошибка сканирования {symbol}: {e}")

                    await asyncio.sleep(0.2)

                if all_setups:
                    await self._process_setups(all_setups)

                logger.info(
                    f"Сканирование #{self.scan_count} завершено. "
                    f"Найдено сетапов: {len(all_setups)}"
                )

            except Exception as e:
                logger.error(f"Ошибка в цикле сканирования: {e}")
                await self.telegram.send_error(str(e))

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    # ──────────────────────────────────────────────────────────────────────────

    async def _scan_symbol(self, symbol: str, current_price: float) -> list[TradeSetup]:
        setups = []

        # Получаем 240m через кэш (не запрашиваем каждые 60 сек)
        df_senior = await self._get_senior_df(symbol)

        for tf_working in ["15", "5"]:
            if not self.signal_cooldown.can_signal(symbol, tf_working):
                continue

            df_working = await self.data_fetcher.get_klines(symbol, tf_working)
            if df_working is None or len(df_working) < 50:
                continue

            found = self.setup_detector.scan_for_setups(
                symbol           = symbol,
                df_working       = df_working,
                timeframe_working = tf_working,
                df_senior        = df_senior,
                timeframe_senior = "240",
                current_price    = current_price,
            )
            setups.extend(found)

        return setups

    # ──────────────────────────────────────────────────────────────────────────

    async def _process_setups(self, setups: list[TradeSetup]):
        quality_order = {"A+": 0, "A": 1, "B": 2, "C": 3}
        setups.sort(key=lambda s: quality_order.get(s.quality, 4))

        for setup in setups:
            if not setup.is_valid:
                continue

            can_trade, reason = self.risk_manager.can_open_trade(setup.symbol)

            if setup.trendline_broken and can_trade:
                # ИСПРАВЛЕНО: сначала открываем сделку, потом шлём сигнал
                trade = TradeRecord(
                    symbol             = setup.symbol,
                    direction          = setup.direction,
                    entry_price        = setup.entry_price,
                    stop_loss          = setup.stop_loss,
                    tp1_price          = setup.tp1_price,
                    tp2_price          = setup.tp2_price,
                    position_size_pct  = setup.position_size_pct,
                    setup_quality      = setup.quality,
                    timeframe          = setup.timeframe,
                    setup_type         = setup.setup_type,
                )

                if self.risk_manager.open_trade(trade):
                    self.position_tracker.add_position(setup, trade)
                    # Сигнал отправляем только при успешном открытии
                    await self.telegram.send_signal(setup)
                    self.signal_cooldown.record_signal(setup.symbol, setup.timeframe)
                else:
                    logger.warning(
                        f"Трейд {setup.symbol} не открыт (risk_manager отказал) — "
                        f"сигнал в Telegram НЕ отправлен"
                    )

            elif not setup.trendline_broken:
                if self.signal_cooldown.can_signal(setup.symbol, f"{setup.timeframe}_forming"):
                    await self.telegram.send_signal(setup)
                    self.signal_cooldown.record_signal(setup.symbol, f"{setup.timeframe}_forming")

    # ──────────────────────────────────────────────────────────────────────────

    async def _position_monitor_loop(self):
        while self.is_running:
            try:
                if self.position_tracker.positions:
                    active_symbols = list(self.position_tracker.positions.keys())
                    prices         = await self.data_fetcher.get_current_prices(active_symbols)

                    if prices:
                        updates = self.position_tracker.check_positions(prices)
                        for update in updates:
                            await self.telegram.send_update(update)

            except Exception as e:
                logger.error(f"Ошибка мониторинга позиций: {e}")

            await asyncio.sleep(10)

    # ──────────────────────────────────────────────────────────────────────────

    async def _daily_report_loop(self):
        last_report_date = None
        while self.is_running:
            try:
                now = datetime.now(timezone.utc)
                if now.hour == 0 and last_report_date != now.date():
                    stats      = self.risk_manager.get_daily_stats()
                    rolling_wr = self.risk_manager.get_rolling_winrate(30)
                    await self.telegram.send_daily_report(stats, rolling_wr)
                    last_report_date = now.date()
            except Exception as e:
                logger.error(f"Ошибка дневного отчёта: {e}")

            await asyncio.sleep(30)
