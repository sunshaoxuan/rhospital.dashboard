#!/usr/bin/env sh
set -eu

cat > /usr/local/sbin/rhdashboard-db-firewall.sh <<'SCRIPT'
#!/usr/bin/env sh
set -eu

source_ip="178.239.117.99"
destination_cidr="172.18.0.0/16"
source_port="35432"

if ! iptables -L MAILCOW -n >/dev/null 2>&1; then
  exit 0
fi

if ! iptables -C MAILCOW -s "$source_ip/32" -d "$destination_cidr" -p tcp --sport "$source_port" -j ACCEPT 2>/dev/null; then
  iptables -I MAILCOW 1 -s "$source_ip/32" -d "$destination_cidr" -p tcp --sport "$source_port" -j ACCEPT
fi
SCRIPT
chmod 0755 /usr/local/sbin/rhdashboard-db-firewall.sh

cat > /etc/systemd/system/rhdashboard-db-firewall.service <<'SERVICE'
[Unit]
Description=Allow rhdashboard container access to production PostgreSQL source
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/rhdashboard-db-firewall.sh

[Install]
WantedBy=multi-user.target
SERVICE

cat > /etc/systemd/system/rhdashboard-db-firewall.timer <<'TIMER'
[Unit]
Description=Recheck rhdashboard production database firewall rule

[Timer]
OnBootSec=15s
OnUnitActiveSec=30s
AccuracySec=5s
Unit=rhdashboard-db-firewall.service
Persistent=true

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable rhdashboard-db-firewall.service >/dev/null
systemctl enable --now rhdashboard-db-firewall.timer >/dev/null
systemctl restart rhdashboard-db-firewall.service

systemctl is-enabled rhdashboard-db-firewall.timer
systemctl is-active rhdashboard-db-firewall.timer
iptables -C MAILCOW -s 178.239.117.99/32 -d 172.18.0.0/16 -p tcp --sport 35432 -j ACCEPT
