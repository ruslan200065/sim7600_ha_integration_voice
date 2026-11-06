#!/command/with-contenv sh
set -eu
cd /app
/opt/venv/bin/python install_integration.py
exec /opt/venv/bin/python -u main.py
