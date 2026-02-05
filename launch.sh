#!/usr/bin/env bash
# DC-Detector v0.2 — Raspberry Pi / Linux launcher
# Usage:  bash launch.sh   or   chmod +x launch.sh && ./launch.sh

set -e
cd "$(dirname "$0")"

echo "============================================================"
echo " DC-Detector v0.2 — Linux / Raspberry Pi launcher"
echo "============================================================"

PYTHON=python3

# Create venv if missing
if [ ! -f "venv/bin/python" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv venv
fi

# Activate
source venv/bin/activate

echo "Installing / updating dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

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
