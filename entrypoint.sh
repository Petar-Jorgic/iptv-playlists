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
exec gunicorn \
  --bind 0.0.0.0:8080 \
  --workers 4 \
  --threads 4 \
  --timeout 120 \
  app:app
