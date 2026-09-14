#!/usr/bin/env bash
# Thin entry point for the weekly Hermes cron job. No logic here — all
# orchestration lives in weekly_run.py.
cd "$(dirname "$0")"
exec python3 weekly_run.py "$@"
