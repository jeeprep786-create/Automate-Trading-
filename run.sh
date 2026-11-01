#!/usr/bin/env bash
# One-command start (Linux / macOS / WSL). Windows: see README.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
