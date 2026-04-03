"""
main.py — entrypoint for Bybit Wave Scanner.

Требования:
- использует config.py как единую точку конфигурации
- поднимает scanner
- корректно завершает сервис по SIGINT/SIGTERM
- централизованно настраивает loguru
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from loguru import logger

from config import (
    APP_ENV,
    DEBUG,
    LOG_LEVEL,
    LOG_FILE,
    LOG_ROTATION,
    LOG_RETENTION,
    LOG_COMPRESSION,
    export_runtime_config,
)
from scanner import Scanner


def configure_logging() -> None:
    log_path = Path(LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger.remove()

    console_level = "DEBUG" if DEBUG else LOG_LEVEL

    logger.add(
        sys.stdout,
        level=console_level,
        enqueue=False,
        backtrace=DEBUG,
        diagnose=DEBUG,
        colorize=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
    )

    logger.add(
        LOG_FILE,
        level=LOG_LEVEL,
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        compression=LOG_COMPRESSION,
        enqueue=True,
        backtrace=False,
        diagnose=False,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | "
            "{level: <8} | "
            "{process}:{thread} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
    )


async def run_service() -> None:
    scanner = Scanner()
    stop_event = asyncio.Event()
    stop_started = False

    async def shutdown(reason: str) -> None:
        nonlocal stop_started
        if stop_started:
            return
        stop_started = True

        logger.warning("Получен сигнал остановки: {}", reason)

        try:
            await scanner.stop()
        except Exception as exc:
            logger.exception("Ошибка при scanner.stop(): {}", exc)
        finally:
            stop_event.set()

    loop = asyncio.get_running_loop()

    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            sig = getattr(signal, sig_name)
            loop.add_signal_handler(
                sig,
                lambda s=sig_name: asyncio.create_task(shutdown(s)),
            )
        except (NotImplementedError, AttributeError):
            # Windows / некоторые event loop реализации
            pass

    scanner_task = asyncio.create_task(scanner.start(), name="scanner_service")

    try:
        done, pending = await asyncio.wait(
            {scanner_task, asyncio.create_task(stop_event.wait(), name="stop_event_waiter")},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in done:
            if task is scanner_task:
                exc = task.exception()
                if exc:
                    raise exc

        if stop_event.is_set() and not scanner_task.done():
            scanner_task.cancel()
            await asyncio.gather(scanner_task, return_exceptions=True)

        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    except asyncio.CancelledError:
        logger.warning("run_service cancelled")
        await shutdown("cancelled")
        raise
    except KeyboardInterrupt:
        logger.warning("Остановка по Ctrl+C")
        await shutdown("keyboard_interrupt")
    except Exception as exc:
        logger.exception("Критическая ошибка сервиса: {}", exc)
        await shutdown(f"exception:{type(exc).__name__}")
        raise
    finally:
        if not stop_event.is_set():
            await shutdown("finalizer")


async def main() -> None:
    configure_logging()

    logger.info("╔══════════════════════════════════════════════════╗")
    logger.info("║            BYBIT WAVE SCANNER                   ║")
    logger.info("║         Elliott Wave + Breakout Engine          ║")
    logger.info("╚══════════════════════════════════════════════════╝")

    runtime_cfg = export_runtime_config(redact_secrets=True)
    logger.info("Environment: {}", APP_ENV)
    logger.info("Runtime config loaded: {}", runtime_cfg)

    await run_service()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass