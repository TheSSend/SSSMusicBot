Lavalink for this bot

Files:
- Lavalink.jar - server binary
- application.yml.example - example config for 127.0.0.1:2333 and password youshallnotpass
- application.yml - local config copied from the example
- start-lavalink.bat - Windows launch script
- start-lavalink.sh - Ubuntu/Linux launch script

Important:
- YouTube plugin is configured in application.yml and downloads automatically on first launch.
- Plugin version: 1.18.0
- Lavalink version: 4.2.2
- For GitHub, commit `application.yml.example`, not your local `application.yml`

Run on Windows:
1. Copy config: copy application.yml.example application.yml
2. Double-click start-lavalink.bat
3. Wait for "Lavalink is ready to accept connections."

Run on Ubuntu 24:
1. Install Java 21+: sudo apt update && sudo apt install openjdk-21-jre-headless
2. Copy config: cp application.yml.example application.yml
3. Make scripts executable: chmod +x start-lavalink.sh ../run-bot.sh
4. Start Lavalink: ./start-lavalink.sh
5. Start bot from project root: ./run-bot.sh

If YouTube playback starts failing later:
- update Lavalink.jar
- update youtube plugin version in application.yml
- optionally add poToken or OAuth settings to plugins.youtube
