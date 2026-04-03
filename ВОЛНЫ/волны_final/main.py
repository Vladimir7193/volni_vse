# ============================================================
# FILE: main.py — полная замена
# ============================================================

"""
Точка входа — запуск Bybit Wave Scanner.
"""

import asyncio
import sys
from loguru import logger

from config import LOG_LEVEL, LOG_FILE
from scanner import Scanner


# ═══════════════════════════════════════════
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ═══════════════════════════════════════════
logger.remove()
logger.add(
    sys.stdout,
    level=LOG_LEVEL,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
           "<level>{level: <8}</level> | "
           "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
           "<level>{message}</level>"
)
logger.add(
    LOG_FILE,
    level="DEBUG",
    rotation="50 MB",
    retention="7 days",
    compression="zip",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}"
)


async def main():
    """Главная функция"""
    logger.info("╔══════════════════════════════════════════╗")
    logger.info("║     BYBIT WAVE SCANNER v1.0              ║")
    logger.info("║     Elliott Wave + Trendline Breakout     ║")
    logger.info("║     50 Coins | USDT Perpetual             ║")
    logger.info("╚══════════════════════════════════════════╝")

    scanner = Scanner()

    # Корректная обработка сигналов в asyncio
    loop = asyncio.get_running_loop()
    for sig_name in ('SIGINT', 'SIGTERM'):
        try:
            import signal
            sig = getattr(signal, sig_name)
            loop.add_signal_handler(sig, lambda: asyncio.create_task(scanner.stop()))
        except (NotImplementedError, AttributeError):
            # Windows не поддерживает add_signal_handler
            pass

    try:
        await scanner.start()
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C...")
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}", exc_info=True)
    finally:
        await scanner.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass