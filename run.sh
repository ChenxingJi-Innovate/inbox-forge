#!/bin/bash
# Start the email summarizer server
set -e

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

if [ ! -f ".venv/.deps-installed" ]; then
    echo "Installing dependencies..."
    pip install --upgrade pip
    pip install -r requirements.txt
    touch .venv/.deps-installed
fi

if [ ! -f ".env" ]; then
    echo "ERROR: .env file not found."
    echo "Run: cp .env.example .env"
    echo "Then fill in your GEMINI_API_KEY and SESSION_SECRET"
    exit 1
fi

echo ""
echo "============================================"
echo " Email Summarizer running at:"
echo "   http://127.0.0.1:8000"
echo " Press Ctrl+C to stop"
echo "============================================"
echo ""

python main.py
