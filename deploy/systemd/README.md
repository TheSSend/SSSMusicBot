# systemd units

These files are templates for Ubuntu 24.

## Expected paths

- Project root: `/opt/sssmusicbot`
- Bot user/group: `musicbot`
- Virtualenv: `/opt/sssmusicbot/.venv`
- Bot logs: `/var/log/sssmusicbot`
- Bot runtime data: `/var/lib/sssmusicbot`
- Web panel: `/opt/sssmusicbot/web_admin.py`

## Install

1. Create a service user:

```bash
sudo useradd -r -m -d /opt/sssmusicbot -s /bin/bash musicbot
```

2. Copy project files to `/opt/sssmusicbot`
3. Create `.env` and `lavalink/application.yml`
4. Create virtualenv and install dependencies
5. Copy unit files:

```bash
sudo cp deploy/systemd/lavalink.service /etc/systemd/system/
sudo cp deploy/systemd/musicbot.service /etc/systemd/system/
sudo cp deploy/systemd/musicbot-web.service /etc/systemd/system/
```

6. Reload and enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lavalink.service
sudo systemctl enable --now musicbot.service
sudo systemctl enable --now musicbot-web.service
```

## Logs

```bash
sudo journalctl -u lavalink -f
sudo journalctl -u musicbot -f
sudo journalctl -u musicbot-web -f
```

## Restart button permissions

If you want the web panel to restart the bot, allow the service user to restart only `musicbot.service`:

```bash
sudo visudo
```

Add a line like:

```text
musicbot ALL=NOPASSWD: /bin/systemctl restart musicbot.service
```

Adjust `User`, `Group`, and `WorkingDirectory` in the unit files if you deploy to a different path.
