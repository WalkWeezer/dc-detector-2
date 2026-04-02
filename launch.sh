#!/usr/bin/env bash
# DC-Detector v0.2 — Raspberry Pi / Linux launcher
# Usage:  bash launch.sh   or   chmod +x launch.sh && ./launch.sh

set -e
cd "$(dirname "$0")"

echo "============================================================"
echo " DC-Detector v0.2 — Linux / Raspberry Pi launcher"
echo "============================================================"

# Auto-update from git repository (only if internet is available)
if git rev-parse --is-inside-work-tree &>/dev/null 2>&1; then
    if ping -c 1 -W 2 8.8.8.8 &>/dev/null 2>&1; then
        echo "Updating repository..."
        rm -f .git/index.lock
        git stash
        git pull
        echo "Repository updated."
        echo ""
    else
        echo "No internet connection — skipping update."
        echo ""
    fi
fi

PYTHON=python3

# Create venv if missing
# --system-site-packages is required on Raspberry Pi so that the venv can
# access picamera2 and libcamera which are installed via apt, not pip.
if [ ! -f "venv/bin/python" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv --system-site-packages venv
fi

# Activate
source venv/bin/activate

echo "Installing / updating dependencies..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Copy example config if needed
if [ ! -f "config.yaml" ]; then
    echo "No config.yaml found — copying from config.example.yaml"
    cp config.example.yaml config.yaml
fi

export PYTHONPATH="$(pwd)/src"

echo ""
echo "Starting DC-Detector..."
echo ""

python src/launcher.py "$@"
