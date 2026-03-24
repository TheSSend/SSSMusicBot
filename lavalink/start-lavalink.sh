#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -f "Lavalink.jar" ]]; then
  echo "Lavalink.jar not found in $ROOT_DIR"
  exit 1
fi

exec java -Xms512M -Xmx512M -Dfile.encoding=UTF-8 -jar Lavalink.jar
