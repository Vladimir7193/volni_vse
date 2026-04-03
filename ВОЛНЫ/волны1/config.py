"""
config.py — centralized runtime configuration for volny scanner.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE if ENV_FILE.exists() else None)


def _get_str(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and (value is None or str(value).strip() == ""):
        raise ValueError(f"Missing required environment variable: {name}")
    return str(value) if value is not None else ""


def _get_int(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Environment variable {name} must be int, got: {raw!r}")
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"{name} must be <= {max_value}, got {value}")
    return value


def _get_float(name: str, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Environment variable {name} must be float, got: {raw!r}")
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"{name} must be <= {max_value}, got {value}")
    return value


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _get_csv_list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


APP_ENV = _get_str("APP_ENV", "production")
DEBUG = _get_bool("DEBUG", False)
TESTNET = _get_bool("BYBIT_TESTNET", False)

BYBIT_API_KEY = _get_str("BYBIT_API_KEY", required=True)
BYBIT_API_SECRET = _get_str("BYBIT_API_SECRET", required=True)

TELEGRAM_BOT_TOKEN = _get_str("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _get_str("TELEGRAM_CHAT_ID", "")
TELEGRAM_PROXY = _get_str("TELEGRAM_PROXY", "")

LOG_LEVEL = _get_str("LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(_get_str("LOG_DIR", str(BASE_DIR)))
LOG_FILE = _get_str("LOG_FILE", str(LOG_DIR / "scanner.log"))
LOG_ROTATION = _get_str("LOG_ROTATION", "50 MB")
LOG_RETENTION = _get_str("LOG_RETENTION", "7 days")
LOG_COMPRESSION = _get_str("LOG_COMPRESSION", "zip")

DEFAULT_SYMBOLS = (
    "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,"
    "AVAXUSDT,LTCUSDT"
)
SYMBOLS = _get_csv_list("SYMBOLS", DEFAULT_SYMBOLS)

BYBIT_INTERVALS = {
    "1": "1",
    "3": "3",
    "5": "5",
    "15": "15",
    "30": "30",
    "60": "60",
    "120": "120",
    "240": "240",
    "360": "360",
    "720": "720",
    "D": "D",
    "W": "W",
}

KLINE_LIMIT = {
    "5": _get_int("KLINE_LIMIT_5", 300, min_value=50, max_value=1000),
    "15": _get_int("KLINE_LIMIT_15", 300, min_value=50, max_value=1000),
    "240": _get_int("KLINE_LIMIT_240", 300, min_value=50, max_value=1000),
}

SCAN_INTERVAL_SECONDS = _get_int("SCAN_INTERVAL_SECONDS", 60, min_value=5, max_value=3600)
SIGNAL_COOLDOWN_MINUTES = _get_int("SIGNAL_COOLDOWN_MINUTES", 90, min_value=1, max_value=1440)
SENIOR_CACHE_TTL_SECONDS = _get_int("SENIOR_CACHE_TTL_SECONDS", 900, min_value=30, max_value=86400)

MIN_RR_RATIO = _get_float("MIN_RR_RATIO", 1.8, min_value=0.5, max_value=20.0)
MAX_STOP_LOSS_PCT = _get_float("MAX_STOP_LOSS_PCT", 3.0, min_value=0.1, max_value=50.0)
MIN_MOVE_PCT = _get_float("MIN_MOVE_PCT", 1.2, min_value=0.1, max_value=50.0)

LEVERAGE = _get_float("LEVERAGE", 5.0, min_value=1.0, max_value=100.0)
RISK_PER_TRADE_PCT = _get_float("RISK_PER_TRADE_PCT", 0.5, min_value=0.05, max_value=10.0)
DAILY_RISK_LIMIT_PCT = _get_float("DAILY_RISK_LIMIT_PCT", 2.0, min_value=0.1, max_value=25.0)
MAX_TRADES_PER_DAY = _get_int("MAX_TRADES_PER_DAY", 8, min_value=1, max_value=100)
MAX_CONCURRENT_TRADES = _get_int("MAX_CONCURRENT_TRADES", 3, min_value=1, max_value=50)

ZIGZAG_SETTINGS = {
    "deviation": _get_float("ZIGZAG_DEVIATION", 3.0, min_value=0.01, max_value=100.0),
    "depth": _get_int("ZIGZAG_DEPTH", 8, min_value=1, max_value=500),
    "backstep": _get_int("ZIGZAG_BACKSTEP", 3, min_value=1, max_value=100),
}

STORE_REJECTED_SETUPS = _get_bool("STORE_REJECTED_SETUPS", True)
STORE_FORMATTING_FEATURES = _get_bool("STORE_FORMATTING_FEATURES", True)
ENABLE_VERBOSE_MARKET_LOGS = _get_bool("ENABLE_VERBOSE_MARKET_LOGS", False)

_VALID_LOG_LEVELS = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}

if LOG_LEVEL not in _VALID_LOG_LEVELS:
    raise ValueError(f"LOG_LEVEL must be one of {_VALID_LOG_LEVELS}, got: {LOG_LEVEL}")

if not SYMBOLS:
    raise ValueError("SYMBOLS cannot be empty")

for tf in ("5", "15", "240"):
    if tf not in KLINE_LIMIT:
        raise ValueError(f"Missing KLINE_LIMIT for timeframe {tf}")

if RISK_PER_TRADE_PCT > DAILY_RISK_LIMIT_PCT:
    raise ValueError(
        f"RISK_PER_TRADE_PCT ({RISK_PER_TRADE_PCT}) cannot exceed DAILY_RISK_LIMIT_PCT ({DAILY_RISK_LIMIT_PCT})"
    )

def export_runtime_config(redact_secrets: bool = True) -> dict[str, Any]:
    return {
        "APP_ENV": APP_ENV,
        "DEBUG": DEBUG,
        "TESTNET": TESTNET,
        "BYBIT_API_KEY": "***" if redact_secrets and BYBIT_API_KEY else BYBIT_API_KEY,
        "BYBIT_API_SECRET": "***" if redact_secrets and BYBIT_API_SECRET else BYBIT_API_SECRET,
        "TELEGRAM_BOT_TOKEN": "***" if redact_secrets and TELEGRAM_BOT_TOKEN else TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": "***" if redact_secrets and TELEGRAM_CHAT_ID else TELEGRAM_CHAT_ID,
        "LOG_LEVEL": LOG_LEVEL,
        "LOG_FILE": LOG_FILE,
        "LOG_ROTATION": LOG_ROTATION,
        "LOG_RETENTION": LOG_RETENTION,
        "LOG_COMPRESSION": LOG_COMPRESSION,
        "SYMBOLS": SYMBOLS,
        "SCAN_INTERVAL_SECONDS": SCAN_INTERVAL_SECONDS,
        "SIGNAL_COOLDOWN_MINUTES": SIGNAL_COOLDOWN_MINUTES,
        "SENIOR_CACHE_TTL_SECONDS": SENIOR_CACHE_TTL_SECONDS,
        "MIN_RR_RATIO": MIN_RR_RATIO,
        "MAX_STOP_LOSS_PCT": MAX_STOP_LOSS_PCT,
        "MIN_MOVE_PCT": MIN_MOVE_PCT,
        "LEVERAGE": LEVERAGE,
        "RISK_PER_TRADE_PCT": RISK_PER_TRADE_PCT,
        "DAILY_RISK_LIMIT_PCT": DAILY_RISK_LIMIT_PCT,
        "MAX_TRADES_PER_DAY": MAX_TRADES_PER_DAY,
        "MAX_CONCURRENT_TRADES": MAX_CONCURRENT_TRADES,
        "STORE_REJECTED_SETUPS": STORE_REJECTED_SETUPS,
        "STORE_FORMATTING_FEATURES": STORE_FORMATTING_FEATURES,
        "ENABLE_VERBOSE_MARKET_LOGS": ENABLE_VERBOSE_MARKET_LOGS,
        "KLINE_LIMIT": KLINE_LIMIT,
        "BYBIT_INTERVALS": BYBIT_INTERVALS,
        "ZIGZAG_SETTINGS": ZIGZAG_SETTINGS,
        "TELEGRAM_PROXY": TELEGRAM_PROXY,
    }