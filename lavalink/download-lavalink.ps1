$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$jarUrl = "https://github.com/lavalink-devs/Lavalink/releases/download/4.2.2/Lavalink.jar"
$jarPath = Join-Path $root "Lavalink.jar"

Write-Host "Downloading Lavalink 4.2.2..."
Invoke-WebRequest -Uri $jarUrl -OutFile $jarPath
Write-Host "Saved to $jarPath"
