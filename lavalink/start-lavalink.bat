@echo off
setlocal
cd /d "%~dp0"

if not exist "Lavalink.jar" (
  echo Lavalink.jar not found in %cd%
  pause
  exit /b 1
)

echo Starting Lavalink on 127.0.0.1:2333...
java --enable-native-access=ALL-UNNAMED -Xms512M -Xmx512M -Dfile.encoding=UTF-8 -jar Lavalink.jar

echo.
echo Lavalink stopped.
pause
