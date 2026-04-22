#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if ! command -v python3 &>/dev/null; then
  echo "python3 not found"; exit 1
fi

if [ ! -d .venv ]; then
  echo "→ Creating virtualenv…"
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "→ Installing dependencies…"
pip install -q -r requirements.txt

echo "→ Installing Playwright Chromium…"
playwright install chromium 2>/dev/null || true

echo ""
echo "✓  Play Store Checker → http://localhost:8000"
echo "   Press Ctrl+C to stop."
echo ""
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
