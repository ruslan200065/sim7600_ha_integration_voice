#!/bin/sh
set -eu
cd /app
exec /opt/venv/bin/python -u main.py
