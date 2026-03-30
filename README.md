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

For `forum_search` on Ubuntu 24, install headless Chromium support:

```bash
sudo apt install chromium-driver chromium
```

Then use the same Quick Start steps above.

## systemd on Ubuntu 24

Template units are included in `deploy/systemd/`.

Default assumptions:

- app path: `/opt/sssmusicbot`
- service user: `musicbot`
- venv path: `/opt/sssmusicbot/.venv`

Install flow:

```bash
sudo useradd -r -m -d /opt/sssmusicbot -s /bin/bash musicbot
sudo mkdir -p /opt/sssmusicbot
sudo chown -R musicbot:musicbot /opt/sssmusicbot
```

Copy the project, then as `musicbot`:

```bash
python3 -m venv /opt/sssmusicbot/.venv
source /opt/sssmusicbot/.venv/bin/activate
pip install -r /opt/sssmusicbot/requirements.txt
cp /opt/sssmusicbot/.env.example /opt/sssmusicbot/.env
cp /opt/sssmusicbot/lavalink/application.yml.example /opt/sssmusicbot/lavalink/application.yml
```

Install units:

```bash
sudo cp /opt/sssmusicbot/deploy/systemd/lavalink.service /etc/systemd/system/
sudo cp /opt/sssmusicbot/deploy/systemd/musicbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lavalink.service
sudo systemctl enable --now musicbot.service
```

The bot service writes logs to `/var/log/sssmusicbot` and runtime data to `/var/lib/sssmusicbot` via systemd.
The Lavalink unit also uses `/var/lib/sssmusicbot/tmp` as writable temp storage for native libraries.

Check status:

```bash
sudo systemctl status lavalink
sudo systemctl status musicbot
sudo journalctl -u lavalink -f
sudo journalctl -u musicbot -f
```

## Project Files

- `bot.py` - main bot entrypoint and slash commands
- `music_core.py` - music player, controls, and voice handshake fix for Lavalink v4
- `ocr_module.py` - OCR-based music parsing
- `lavalink/application.yml.example` - example Lavalink config
- `.env.example` - example environment variables

For youtube-source plugin based playback, use `lavalink.server.sources.youtube: false` and `plugins.youtube.enabled: true`.

### OCR tuning

`/playimage` uses EasyOCR in a separate one-shot worker process so heavy OCR initialization does not crash the main bot process.

## Notes For GitHub

- Do not commit `.env`
- Do not commit runtime JSON/DB files or logs
- Do not commit `lavalink/Lavalink.jar` or downloaded plugins
- Rotate your Discord token before publishing if it has ever been exposed
