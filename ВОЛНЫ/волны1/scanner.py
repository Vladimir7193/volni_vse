"""
scanner.py — production-oriented orchestration layer.

Совместим по внешним зависимостям:
- SetupDetector
- RiskManager / TradeRecord
- PositionTracker
- TelegramSender
- utils.RateLimiter / SignalCooldown

Основные улучшения:
- bounded concurrency по символам
- защита от overlap scan-loop
- более чистый lifecycle задач
- безопасная обработка stop/start
- дедупликация сетапов в пределах прохода
- более детальные логи latency
"""

from __future__ import annotations

import asyncio
import functools
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Iterable

import pandas as pd
from loguru import logger
from pybit.unified_trading import HTTP

from config import (
    BYBIT_API_KEY,
    BYBIT_API_SECRET,
    SYMBOLS,
    BYBIT_INTERVALS,
    KLINE_LIMIT,
    SCAN_INTERVAL_SECONDS,
    SIGNAL_COOLDOWN_MINUTES,
    SENIOR_CACHE_TTL_SECONDS,
)
from setups import SetupDetector, TradeSetup
from risk_manager import RiskManager, TradeRecord
from position_tracker import PositionTracker
from telegram_bot import TelegramSender
from utils import RateLimiter, SignalCooldown


SCAN_CONCURRENCY = 6
MONITOR_INTERVAL_SECONDS = 10
REPORT_LOOP_INTERVAL_SECONDS = 30
SENIOR_TIMEFRAME = "240"
WORKING_TIMEFRAMES = ("15",)   # <-- убрали "5"

# Порог фильтрации качества
MIN_QUALITY_FOR_SIGNAL = "A+"
MIN_SCORE_FOR_SIGNAL = 90.0


@dataclass(slots=True)
class SeniorCacheEntry:
    fetched_at_monotonic: float
    df: pd.DataFrame


class BybitDataFetcher:
    def __init__(self) -> None:
        self.client = HTTP(
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
            testnet=False,
        )
        self.rate_limiter = RateLimiter(max_calls=10, period=1.0)

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
    ) -> Optional[pd.DataFrame]:
        try:
            await self.rate_limiter.acquire()

            bybit_interval = BYBIT_INTERVALS.get(interval, interval)
            actual_limit = KLINE_LIMIT.get(interval, limit)

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                functools.partial(
                    self.client.get_kline,
                    category="linear",
                    symbol=symbol,
                    interval=bybit_interval,
                    limit=actual_limit,
                ),
            )

            if response.get("retCode") != 0:
                logger.error(
                    "Bybit API error on klines: symbol={} interval={} msg={}",
                    symbol,
                    interval,
                    response.get("retMsg"),
                )
                return None

            klines = response.get("result", {}).get("list", [])
            if not klines:
                return None

            df = pd.DataFrame(
                klines,
                columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
            )

            numeric_columns = ["timestamp", "open", "high", "low", "close", "volume"]
            for col in numeric_columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.dropna(subset=numeric_columns)
            if df.empty:
                return None

            df = df.sort_values("timestamp").reset_index(drop=True)
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df.set_index("datetime", inplace=True)
            return df

        except Exception as exc:
            logger.exception("Ошибка загрузки kline {} {}: {}", symbol, interval, exc)
            return None

    async def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        try:
            await self.rate_limiter.acquire()
            symbols_set = set(symbols)

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                functools.partial(self.client.get_tickers, category="linear"),
            )

            if response.get("retCode") != 0:
                logger.error("Bybit API error on tickers: {}", response.get("retMsg"))
                return prices

            for ticker in response.get("result", {}).get("list", []):
                symbol = ticker.get("symbol")
                if symbol in symbols_set:
                    try:
                        prices[symbol] = float(ticker["lastPrice"])
                    except (TypeError, ValueError, KeyError):
                        continue

        except Exception as exc:
            logger.exception("Ошибка получения текущих цен: {}", exc)

        return prices

    async def get_current_price(self, symbol: str) -> float:
        prices = await self.get_current_prices([symbol])
        return prices.get(symbol, 0.0)


class Scanner:
    def __init__(self) -> None:
        self.data_fetcher = BybitDataFetcher()
        self.setup_detector = SetupDetector()
        self.risk_manager = RiskManager()
        self.position_tracker = PositionTracker(self.risk_manager)
        self.telegram = TelegramSender()
        self.signal_cooldown = SignalCooldown(SIGNAL_COOLDOWN_MINUTES)

        self.is_running = False
        self.scan_count = 0

        self._senior_cache: dict[str, SeniorCacheEntry] = {}
        self._tasks: list[asyncio.Task] = []
        self._scan_guard = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._symbol_scan_semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def start(self) -> None:
        async with self._state_lock:
            if self.is_running:
                logger.warning("Scanner.start() вызван повторно — уже запущен")
                return
            self.is_running = True

        logger.info("═══ BYBIT WAVE SCANNER ЗАПУЩЕН ═══")
        await self._safe_send_startup()

        self._tasks = [
            asyncio.create_task(self._scan_loop(), name="scan_loop"),
            asyncio.create_task(self._position_monitor_loop(), name="position_monitor"),
            asyncio.create_task(self._daily_report_loop(), name="daily_report"),
        ]

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Критическая ошибка главного lifecycle scanner: {}", exc)
            await self._safe_send_error(f"Критическая ошибка scanner lifecycle: {exc}")
        finally:
            await self._cancel_background_tasks()

    async def stop(self) -> None:
        async with self._state_lock:
            if not self.is_running:
                return
            self.is_running = False

        logger.info("═══ BYBIT WAVE SCANNER ОСТАНОВЛЕН ═══")
        await self._cancel_background_tasks()

    async def _cancel_background_tasks(self) -> None:
        current = asyncio.current_task()
        active_tasks = [t for t in self._tasks if t is not None and t is not current and not t.done()]

        for task in active_tasks:
            task.cancel()

        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

        self._tasks = []

    async def _safe_send_startup(self) -> None:
        try:
            await self.telegram.send_startup_message()
        except Exception as exc:
            logger.error("Не удалось отправить startup message: {}", exc)

    async def _safe_send_error(self, message: str) -> None:
        try:
            await self.telegram.send_error(message)
        except Exception as exc:
            logger.error("Не удалось отправить error message в Telegram: {}", exc)

    async def _get_senior_df(self, symbol: str) -> Optional[pd.DataFrame]:
        now = time.monotonic()
        cached = self._senior_cache.get(symbol)

        if cached and (now - cached.fetched_at_monotonic) < SENIOR_CACHE_TTL_SECONDS:
            return cached.df

        df = await self.data_fetcher.get_klines(symbol, SENIOR_TIMEFRAME)
        if df is not None:
            self._senior_cache[symbol] = SeniorCacheEntry(
                fetched_at_monotonic=now,
                df=df,
            )
        return df

    async def _scan_loop(self) -> None:
        while self.is_running:
            cycle_started = time.perf_counter()

            try:
                if self._scan_guard.locked():
                    logger.warning("Предыдущий scan-cycle ещё не завершён — текущий проход пропущен")
                    await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                    continue

                async with self._scan_guard:
                    self.scan_count += 1
                    logger.info("═══ Сканирование #{} ═══", self.scan_count)
                    await self._run_single_scan_cycle()

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Ошибка в scan loop: {}", exc)
                await self._safe_send_error(f"Ошибка в цикле сканирования: {exc}")

            elapsed = time.perf_counter() - cycle_started
            sleep_for = max(0.0, SCAN_INTERVAL_SECONDS - elapsed)

            logger.info(
                "Сканирование #{} завершено за {:.2f}s, sleep {:.2f}s",
                self.scan_count,
                elapsed,
                sleep_for,
            )
            await asyncio.sleep(sleep_for)

    async def _run_single_scan_cycle(self) -> None:
        prices = await self.data_fetcher.get_current_prices(SYMBOLS)
        if not prices:
            logger.warning("Не удалось получить текущие цены ни по одному символу")
            return

        scan_tasks = [
            asyncio.create_task(self._scan_symbol_guarded(symbol, prices[symbol]))
            for symbol in SYMBOLS
            if symbol in prices
        ]

        if not scan_tasks:
            logger.warning("Нет символов для сканирования в текущем цикле")
            return

        results = await asyncio.gather(*scan_tasks, return_exceptions=True)

        all_setups: list[TradeSetup] = []
        for result in results:
            if isinstance(result, Exception):
                logger.error("Одна из задач сканирования завершилась ошибкой: {}", result)
                continue
            all_setups.extend(result)

        deduped = self._deduplicate_setups(all_setups)
        filtered_setups = self._filter_high_quality_setups(deduped)

        logger.info(
            "Сканирование #{}: raw_setups={} deduped={} filtered_setups={}",
            self.scan_count,
            len(all_setups),
            len(deduped),
            len(filtered_setups),
        )

        if filtered_setups:
            await self._process_setups(filtered_setups)

    def _filter_high_quality_setups(self, setups: list[TradeSetup]) -> list[TradeSetup]:
        """Оставляем только A+ сетапы с score >= MIN_SCORE_FOR_SIGNAL."""
        filtered = [
            s for s in setups
            if getattr(s, "quality", "") == MIN_QUALITY_FOR_SIGNAL
            and getattr(s, "score", 0.0) >= MIN_SCORE_FOR_SIGNAL
        ]
        logger.debug(
            "_filter_high_quality_setups: in={} out={}",
            len(setups),
            len(filtered),
        )
        return filtered

    async def _scan_symbol_guarded(self, symbol: str, current_price: float) -> list[TradeSetup]:
        async with self._symbol_scan_semaphore:
            started = time.perf_counter()
            try:
                setups = await self._scan_symbol(symbol, current_price)
                elapsed = time.perf_counter() - started
                if setups:
                    logger.info(
                        "scan_symbol {}: {} setups найдено за {:.2f}s",
                        symbol,
                        len(setups),
                        elapsed,
                    )
                return setups
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Ошибка сканирования символа {}: {}", symbol, exc)
                return []

    async def _scan_symbol(self, symbol: str, current_price: float) -> list[TradeSetup]:
        setups: list[TradeSetup] = []

        df_senior = await self._get_senior_df(symbol)

        for tf_working in WORKING_TIMEFRAMES:
            if not self.signal_cooldown.can_signal(symbol, tf_working):
                continue

            df_working = await self.data_fetcher.get_klines(symbol, tf_working)
            if df_working is None or len(df_working) < 50:
                continue

            found = self.setup_detector.scan_for_setups(
                symbol=symbol,
                df_working=df_working,
                timeframe_working=tf_working,
                df_senior=df_senior,
                timeframe_senior=SENIOR_TIMEFRAME,
                current_price=current_price,
            )

            if found:
                setups.extend(found)

        return setups

    def _deduplicate_setups(self, setups: Iterable[TradeSetup]) -> list[TradeSetup]:
        quality_order = {"A+": 0, "A": 1, "B": 2, "C": 3}
        best_by_key: dict[tuple, TradeSetup] = {}

        for setup in setups:
            key = (
                getattr(setup, "symbol", None),
                getattr(setup, "timeframe", None),
                getattr(setup, "direction", None),
                getattr(setup, "setup_type", None),
                round(float(getattr(setup, "entry_price", 0.0) or 0.0), 6),
            )
            current = best_by_key.get(key)
            if current is None:
                best_by_key[key] = setup
                continue

            current_rank = quality_order.get(getattr(current, "quality", "C"), 99)
            new_rank = quality_order.get(getattr(setup, "quality", "C"), 99)

            if new_rank < current_rank:
                best_by_key[key] = setup

        ordered = list(best_by_key.values())
        ordered.sort(key=lambda s: quality_order.get(getattr(s, "quality", "C"), 99))
        return ordered

    async def _process_setups(self, setups: list[TradeSetup]) -> None:
        for setup in setups:
            try:
                if not getattr(setup, "is_valid", False):
                    continue

                can_trade, reason = self.risk_manager.can_open_trade(setup.symbol)
                if not can_trade:
                    logger.info("Trade skipped for {}: {}", setup.symbol, reason)

                if getattr(setup, "trendline_broken", False) and can_trade:
                    trade = TradeRecord(
                        symbol=setup.symbol,
                        direction=setup.direction,
                        entry_price=setup.entry_price,
                        stop_loss=setup.stop_loss,
                        tp1_price=setup.tp1_price,
                        tp2_price=setup.tp2_price,
                        position_size_pct=setup.position_size_pct,
                        setup_quality=setup.quality,
                        timeframe=setup.timeframe,
                        setup_type=setup.setup_type,
                    )

                    opened = self.risk_manager.open_trade(trade)
                    if not opened:
                        logger.warning(
                            "RiskManager отказал в открытии сделки: {} {} {}",
                            setup.symbol,
                            setup.timeframe,
                            setup.setup_type,
                        )
                        continue

                    self.position_tracker.add_position(setup, trade)

                    try:
                        await self.telegram.send_signal(setup)
                    except Exception as exc:
                        logger.error(
                            "Сделка открыта, но сигнал в Telegram не отправлен: {} {}",
                            setup.symbol,
                            exc,
                        )

                    self.signal_cooldown.record_signal(setup.symbol, setup.timeframe)
                    logger.success(
                        "Trade opened: symbol={} tf={} type={} quality={}",
                        setup.symbol,
                        setup.timeframe,
                        setup.setup_type,
                        setup.quality,
                    )

                elif not getattr(setup, "trendline_broken", False):
                    forming_key = f"{setup.timeframe}_forming"
                    if self.signal_cooldown.can_signal(setup.symbol, forming_key):
                        try:
                            await self.telegram.send_signal(setup)
                            self.signal_cooldown.record_signal(setup.symbol, forming_key)
                        except Exception as exc:
                            logger.error(
                                "Ошибка отправки forming-сигнала {} {}: {}",
                                setup.symbol,
                                setup.timeframe,
                                exc,
                            )

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Ошибка обработки setup {}: {}", getattr(setup, "symbol", "?"), exc)

    async def _position_monitor_loop(self) -> None:
        while self.is_running:
            try:
                positions = getattr(self.position_tracker, "positions", {})
                if positions:
                    active_symbols = list(positions.keys())
                    prices = await self.data_fetcher.get_current_prices(active_symbols)

                    if prices:
                        updates = self.position_tracker.check_positions(prices)
                        for update in updates:
                            try:
                                await self.telegram.send_update(update)
                            except Exception as exc:
                                logger.error("Ошибка отправки update в Telegram: {}", exc)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Ошибка мониторинга позиций: {}", exc)

            await asyncio.sleep(MONITOR_INTERVAL_SECONDS)

    async def _daily_report_loop(self) -> None:
        last_report_date = None

        while self.is_running:
            try:
                now = datetime.now(timezone.utc)
                if now.hour == 0 and last_report_date != now.date():
                    stats = self.risk_manager.get_daily_stats()
                    rolling_wr = self.risk_manager.get_rolling_winrate(30)

                    try:
                        await self.telegram.send_daily_report(stats, rolling_wr)
                        last_report_date = now.date()
                    except Exception as exc:
                        logger.error("Ошибка отправки daily report: {}", exc)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Ошибка daily report loop: {}", exc)

            await asyncio.sleep(REPORT_LOOP_INTERVAL_SECONDS)
