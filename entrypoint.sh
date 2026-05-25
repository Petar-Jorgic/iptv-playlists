#!/bin/sh
set -e

echo "[*] Starting WireGuard..."
wg-quick down wg0 2>/dev/null || true
wg-quick up wg0

echo "[*] Setting DNS..."
printf "nameserver 10.2.0.1\nnameserver 1.1.1.1\nnameserver 8.8.8.8\n" > /etc/resolv.conf

echo "[*] Verifying VPN tunnel..."
PUBLIC_IP=$(wget -qO- https://ifconfig.me || echo "unknown")
echo "[*] Public IP: $PUBLIC_IP"

echo "[*] Starting IPTV proxy on :8080..."
# MUST be 1 worker: pink.rs (and similar) tie the HLS chunklist token to the
# requests.Session cookie jar. Multiple workers = multiple jars, so ffmpeg's
# follow-up chunklist fetch can hit a worker without the cookie -> source returns
# a fresh master/token instead of segments -> endless playlist-refresh loop, no
# .ts output, player buffers forever. One worker + many threads keeps the session
# shared while still serving concurrent streams.
exec gunicorn \
  --bind 0.0.0.0:8080 \
  --workers "${GUNICORN_WORKERS:-1}" \
  --threads "${GUNICORN_THREADS:-16}" \
  --timeout 120 \
  app:app
