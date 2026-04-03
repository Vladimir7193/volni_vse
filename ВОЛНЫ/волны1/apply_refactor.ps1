# Всегда работаем из текущей папки
$base = Get-Location

Write-Host "Using base path: $base"

# 1) config.py
Set-Content -Path "$base\config.py" -Encoding UTF8 -Value @'
[ТУТ ВСТАВЬ СОДЕРЖИМОЕ config.py]
'@

# 2) main.py
Set-Content -Path "$base\main.py" -Encoding UTF8 -Value @'
[ТУТ ВСТАВЬ СОДЕРЖИМОЕ main.py]
'@

# 3) scanner.py
Set-Content -Path "$base\scanner.py" -Encoding UTF8 -Value @'
[ТУТ ВСТАВЬ СОДЕРЖИМОЕ scanner.py]
'@

# 4) risk_manager.py
Set-Content -Path "$base\risk_manager.py" -Encoding UTF8 -Value @'
[ТУТ ВСТАВЬ СОДЕРЖИМОЕ risk_manager.py]
'@

# 5) setups.py
Set-Content -Path "$base\setups.py" -Encoding UTF8 -Value @'
[ТУТ ВСТАВЬ СОДЕРЖИМОЕ setups.py]
'@

# 6) position_tracker.py
Set-Content -Path "$base\position_tracker.py" -Encoding UTF8 -Value @'
[ТУТ ВСТАВЬ СОДЕРЖИМОЕ position_tracker.py]
'@

# 7) telegram_bot.py
Set-Content -Path "$base\telegram_bot.py" -Encoding UTF8 -Value @'
[ТУТ ВСТАВЬ СОДЕРЖИМОЕ telegram_bot.py]
'@

# 8) trendline.py
Set-Content -Path "$base\trendline.py" -Encoding UTF8 -Value @'
[ТУТ ВСТАВЬ СОДЕРЖИМОЕ trendline.py]
'@

# 9) wave_analyzer.py
Set-Content -Path "$base\wave_analyzer.py" -Encoding UTF8 -Value @'
[ТУТ ВСТАВЬ СОДЕРЖИМОЕ wave_analyzer.py]
'@