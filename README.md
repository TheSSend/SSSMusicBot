# Musicbot

Discord bot with music playback, OCR track parsing, giveaways, signups, forum tracking, and utility modules.

## Features

- Music playback through Lavalink + Wavelink
- OCR-based `/playimage` command via EasyOCR
- Giveaways and signups stored locally in JSON
- Forum complaint tracking with SQLite
- Utility modules for announcements and family applications

## Requirements

- Python 3.11+
- Java 21+ for Lavalink
- Discord bot token and required server permissions

## Quick Start

1. Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Create `.env` from `.env.example` and fill in your values.

3. Create Lavalink config:

```bash
cp lavalink/application.yml.example lavalink/application.yml
```

4. Start Lavalink:

```bash
./lavalink/start-lavalink.sh
```

5. Start the bot:

```bash
./run-bot.sh
```

## Windows

- Start Lavalink with `lavalink/start-lavalink.bat`
- Start bot with `python bot.py`

## Ubuntu 24

Install Java:

```bash
sudo apt update
sudo apt install openjdk-21-jre-headless
```

Optional but recommended for performance:

```bash
sudo apt install build-essential python3-dev
```

Then use the same Quick Start steps above.

## Project Files

- `bot.py` - main bot entrypoint and slash commands
- `music_core.py` - music player, controls, and voice handshake fix for Lavalink v4
- `ocr_module.py` - OCR-based music parsing
- `lavalink/application.yml.example` - example Lavalink config
- `.env.example` - example environment variables

## Notes For GitHub

- Do not commit `.env`
- Do not commit runtime JSON/DB files or logs
- Do not commit `lavalink/Lavalink.jar` or downloaded plugins
- Rotate your Discord token before publishing if it has ever been exposed
