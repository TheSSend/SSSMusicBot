# Musicbot

Discord bot with music playback, OCR track parsing, giveaways, signups, and utility modules.

## Features

- Music playback through Lavalink + Wavelink
- OCR-based `/playimage` command via PaddleOCR
- Giveaways and signups stored locally in JSON (crash-safe atomic writes)
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
- `ocr_module.py` - OCR-based music parsing via PaddleOCR
- `config.py` - shared configuration (OWNER_ID, timezones)
- `json_store.py` - crash-safe JSON file storage with atomic writes
- `lavalink/application.yml.example` - example Lavalink config
- `.env.example` - example environment variables

For youtube-source plugin based playback, use `lavalink.server.sources.youtube: false` and `plugins.youtube.enabled: true`.

### OCR tuning

`/playimage` now uses `PaddleOCR` for better multilingual recognition, especially for Cyrillic-heavy screenshots and song lists.

Recommended Ubuntu install:

```bash
source .venv/bin/activate
python -m pip uninstall -y rapidocr-onnxruntime paddleocr paddlepaddle
python -m pip install -U paddlepaddle==3.2.0 paddleocr==3.3.3
```

If you want a clean reinstall:

```bash
python -m pip uninstall -y paddleocr paddlepaddle rapidocr-onnxruntime
python -m pip install -U paddlepaddle==3.2.0 paddleocr==3.3.3
```

If pip cannot find a compatible `paddlepaddle` wheel, use the official CPU index:

```bash
python -m pip install -U --index-url https://www.paddlepaddle.org.cn/packages/stable/cpu/ paddlepaddle==3.2.0 paddleocr==3.3.3
```

## Notes For GitHub

- Do not commit `.env`
- Do not commit runtime JSON/DB files or logs
- Do not commit `lavalink/Lavalink.jar` or downloaded plugins
- Rotate your Discord token before publishing if it has ever been exposed

## Web Admin Panel

The admin panel now runs as a separate process (`musicbot-web.service`) so it stays online when the bot restarts.

Enable in `.env`:

- `WEB_ADMIN_ENABLED=1`
- `WEB_ADMIN_HOST=0.0.0.0` (bind to all interfaces for remote access)
- `WEB_ADMIN_PORT=8080`
- `WEB_ADMIN_TOKEN=...` (required)
- `WEB_ADMIN_BASIC_USER=...` / `WEB_ADMIN_BASIC_PASSWORD=...` for browser login prompt
- `WEB_ADMIN_RESTART_COMMAND=sudo -n /usr/bin/systemctl restart musicbot.service` (optional, used by the Restart button)

Open in browser:

- `http://<server-ip>:8080/?token=<WEB_ADMIN_TOKEN>`
- Or, if Basic Auth is configured, open `http://<server-ip>:8080/` and sign in via the browser prompt

The panel uses shared runtime files:

- `player_state.json` for music resume/queue state
- `panel_state.json` for Discord guild/role/channel name snapshots
- `admin_commands.json` for queued reload/sync actions
- `web_config.json` for module overrides

Deployment:

- bot service: `musicbot.service`
- panel service: `musicbot-web.service`
- panel start command: `python web_admin.py` or the `musicbot-web.service` unit

Security notes:

- Use a strong token and keep it private.
- If you expose restart functionality, lock down `WEB_ADMIN_RESTART_COMMAND` with sudoers so it can only restart `musicbot.service`.
- Install the helper sudoers file from `deploy/systemd/musicbot-restart.sudoers` to avoid password prompts when using the Restart button. The panel now tries both `/usr/bin/systemctl` and `/bin/systemctl` for compatibility.
- Prefer running behind a reverse proxy with HTTPS if exposing to the internet.

## Lyrics source fallback

If `/text` still misses some tracks, you can enable Genius fallback:

- install dependencies with `pip install -r requirements.txt`
- set `GENIUS_ACCESS_TOKEN` in `.env`

This uses the `lyricsgenius` client as an additional source before the older public fallbacks.
