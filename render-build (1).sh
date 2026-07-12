#!/usr/bin/env bash
# render-build.sh
# Installs Google Chrome into a local, non-root-writable directory during
# Render's build step (Render's free/starter instances don't let you apt-get
# install system packages at runtime, and there's no Docker on this plan).
#
# Chrome ends up at:
#   /opt/render/project/.render/chrome/opt/google/chrome/chrome
#
# Render caches the project directory between deploys, so this script skips
# the download if Chrome is already present.

set -o errexit

echo "==> render-build.sh starting"

# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install Chrome (skip if cached from a previous build)
CHROME_DIR="$HOME/project/.render/chrome"
CHROME_BIN_PATH="$CHROME_DIR/opt/google/chrome/chrome"

if [[ ! -f "$CHROME_BIN_PATH" ]]; then
  echo "==> Installing Google Chrome (stable) into $CHROME_DIR"
  mkdir -p "$CHROME_DIR"
  cd "$CHROME_DIR"

  wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb

  # Unpack the .deb without root / dpkg (Render build containers don't give
  # you sudo). `dpkg -x` extracts the archive contents into the target dir.
  dpkg -x google-chrome-stable_current_amd64.deb "$CHROME_DIR"
  rm google-chrome-stable_current_amd64.deb

  cd - >/dev/null
else
  echo "==> Chrome already cached at $CHROME_BIN_PATH, skipping download"
fi

echo "==> Chrome binary path: $CHROME_BIN_PATH"
echo "==> render-build.sh finished"
