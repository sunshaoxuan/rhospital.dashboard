#!/usr/bin/env sh
set -eu

conf="/etc/nginx/conf.d/xray.conf"
backup="/etc/nginx/conf.d/xray.conf.bak.rhdashboard-$(date -u +%Y%m%d%H%M%S)"

if grep -q "location /rhdashboard/" "$conf"; then
  nginx -t
  systemctl reload nginx
  exit 0
fi

cp "$conf" "$backup"

python3 - "$conf" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
insert = """
    location = /rhdashboard {
        add_header Strict-Transport-Security "max-age=0" always;
        return 301 /rhdashboard/;
    }

    location /rhdashboard/ {
        add_header Strict-Transport-Security "max-age=0" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "same-origin" always;
        proxy_pass http://127.0.0.1:18091/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

"""
marker = "    location / {\n"
if marker not in text:
    raise SystemExit("location / marker not found")
path.write_text(text.replace(marker, insert + marker, 1))
PY

nginx -t
systemctl reload nginx

echo "backup: $backup"
