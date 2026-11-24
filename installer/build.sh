#!/usr/bin/env bash
# Build ServerAssistant-Setup.exe (needs NSIS: apt install nsis) and the update zip.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf installer/stage && mkdir -p installer/stage
cp -r app run.py tray.py appwindow.py fix-downloader.ps1 requirements.txt requirements-optional.txt config.example.toml README.md VERSION installer/stage/
find installer/stage -name __pycache__ -prune -exec rm -rf {} +
rm -f installer/stage/app/*.db
cd installer

VER=$(cat ../VERSION)
makensis -V2 -DVERSION="$VER" ServerAssistant.nsi
mv ServerAssistant-Setup.exe ../../ServerAssistant-Setup.exe
rm -rf stage
makensis -V2 -DVERSION="$VER" Viewer.nsi
mv ServerAssistant-Viewer-Setup.exe ../../ServerAssistant-Viewer-Setup.exe
echo built ../ServerAssistant-Setup.exe and the Viewer
