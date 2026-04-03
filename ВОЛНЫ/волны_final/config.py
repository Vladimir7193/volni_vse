"""
config.py — конфигурация Bybit Wave Scanner.
Все чувствительные данные читаются из .env / переменных окружения.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════
# API КЛЮЧИ — только через .env!
# ═══════════════════════════════════════════
BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY",    "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")
TELEGRAM_PROXY     = os.getenv("TELEGRAM_PROXY",     "")  # socks5://user:pass@host:port

# ═══════════════════════════════════════════
# СПИСОК МОНЕТ
# ═══════════════════════════════════════════
COINS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "DOT", "MATIC",
    "LINK", "UNI", "ATOM", "LTC", "FIL", "APT", "ARB", "OP", "SUI", "NEAR",
    "INJ", "TIA", "SEI", "FET", "RNDR", "WLD", "JUP", "PEPE", "WIF", "BONK",
    "ORDI", "STX", "IMX", "MANTA", "TRX", "BCH", "ETC", "AAVE", "MKR", "SNX",
    "CRV", "DYDX", "GMX", "PENDLE", "JTO", "PYTH", "STRK", "BLUR", "ENS", "LDO",
]
SYMBOLS = [f"{coin}USDT" for coin in COINS]

# ═══════════════════════════════════════════
# ТАЙМФРЕЙМЫ
# "7m" убран — Bybit не поддерживает, заменён на "5m"
# ═══════════════════════════════════════════
TIMEFRAMES = {
    "senior":  ["240", "D"],   # 4H, 1D — направление
    "working": ["15", "5"],    # 15m, 5m — сетапы (было: ["15", "7"])
    "entry":   ["1", "3"],     # 1m, 3m — точный вход
}

# Bybit interval mapping (только поддерживаемые интервалы)
BYBIT_INTERVALS = {
    "1":   "1",
    "3":   "3",
    "5":   "5",
    "15":  "15",
    "30":  "30",
    "60":  "60",
    "240": "240",
    "D":   "D",
    "W":   "W",
}

# Количество свечей для загрузки
KLINE_LIMIT = {
    "1":   500,
    "3":   500,
    "5":   500,
    "15":  400,
    "30":  300,
    "60":  300,
    "240": 200,
    "D":   200,
    "W":   200,
}

# ═══════════════════════════════════════════
# ZIGZAG
# ═══════════════════════════════════════════
ZIGZAG_SETTINGS = {
    "1":   {"deviation": 3,  "depth": 7,  "backstep": 2},
    "3":   {"deviation": 4,  "depth": 8,  "backstep": 2},
    "5":   {"deviation": 4,  "depth": 8,  "backstep": 3},
    "15":  {"deviation": 5,  "depth": 10, "backstep": 3},
    "30":  {"deviation": 5,  "depth": 10, "backstep": 3},
    "60":  {"deviation": 5,  "depth": 10, "backstep": 3},
    "240": {"deviation": 7,  "depth": 12, "backstep": 5},
    "D":   {"deviation": 10, "depth": 15, "backstep": 5},
}

# ═══════════════════════════════════════════
# МАНИ-МЕНЕДЖМЕНТ
# ═══════════════════════════════════════════
LEVERAGE            = int(os.getenv("LEVERAGE", "5"))      # плечо (1–20)
MAX_DAILY_LOSS_PCT  = float(os.getenv("MAX_DAILY_LOSS_PCT", "1.5"))  # дневной лимит убытка, %
MAX_DAILY_TRADES    = int(os.getenv("MAX_DAILY_TRADES", "3"))        # макс сделок/день
RISK_PER_TRADE_PCT  = float(os.getenv("RISK_PER_TRADE_PCT", "0.5")) # риск/сделку, % депозита
MIN_RR_RATIO        = 1.5         # минимальный R:R
TARGET_RR_RATIO     = 2.0         # целевой R:R
MAX_STOP_LOSS_PCT   = 4.0         # макс стоп-лосс, %
TP1_CLOSE_PCT       = 70          # % позиции на ТП1
TP2_CLOSE_PCT       = 30          # % позиции на ТП2
MAX_RE_ENTRIES      = 2           # макс перезаходов

# ═══════════════════════════════════════════
# ВОЛНОВОЙ АНАЛИЗ
# ═══════════════════════════════════════════
WAVE_RULES = {
    "wave3_min_extension":      1.0,
    "wave3_typical_ext_min":    1.618,
    "wave3_typical_ext_max":    2.272,
    "wave5_typical_equality":   1.0,
    "correction_levels":        [0.382, 0.500, 0.618, 0.764],
    "tp1_fib":                  0.382,
    "tp2_fib_min":              0.500,
    "tp2_fib_max":              0.618,
}

# ═══════════════════════════════════════════
# СКАНЕР
# ═══════════════════════════════════════════
SCAN_INTERVAL_SECONDS    = 60
MIN_MOVE_PCT             = 1.0
SIGNAL_COOLDOWN_MINUTES  = 30
SENIOR_CACHE_TTL_SECONDS = 900   # кэш 240m данных = 15 мин

# ═══════════════════════════════════════════
# ЛОГИРОВАНИЕ
# ═══════════════════════════════════════════
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE  = "scanner.log"
