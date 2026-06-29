#!/usr/bin/env sh
set -eu

conf_dir="/opt/1panel/www/conf.d"
conf="$conf_dir/rhdashboard.conf"
backup="$conf.bak.$(date -u +%Y%m%d%H%M%S)"

mkdir -p "$conf_dir"

if [ -f "$conf" ]; then
  cp "$conf" "$backup"
fi

cat > "$conf" <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name 178.239.117.99 ccnode.briconbric.com;

    location = /rhdashboard {
        return 301 /rhdashboard/;
    }

    location /rhdashboard/ {
        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "same-origin" always;
        proxy_pass http://127.0.0.1:18091/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

docker exec openresty openresty -t
docker exec openresty openresty -s reload

echo "configured: $conf"
if [ -f "$backup" ]; then
  echo "backup: $backup"
fi
