#!/usr/bin/env bash
set -euo pipefail

ccnode_ip="${1:-203.24.89.50}"
api_port="${2:-18092}"
rule_script="/usr/local/sbin/rhospital-statistics-firewall.sh"
service_file="/etc/systemd/system/rhospital-statistics-firewall.service"

cat > "$rule_script" <<SCRIPT
#!/usr/bin/env bash
set -euo pipefail
iptables -C INPUT -p tcp -s 127.0.0.0/8 --dport ${api_port} -j ACCEPT 2>/dev/null \
  || iptables -I INPUT 1 -p tcp -s 127.0.0.0/8 --dport ${api_port} -j ACCEPT
iptables -C INPUT -p tcp -s ${ccnode_ip}/32 --dport ${api_port} -j ACCEPT 2>/dev/null \
  || iptables -I INPUT 1 -p tcp -s ${ccnode_ip}/32 --dport ${api_port} -j ACCEPT
iptables -C INPUT -p tcp --dport ${api_port} -j DROP 2>/dev/null \
  || iptables -A INPUT -p tcp --dport ${api_port} -j DROP
SCRIPT
chmod 0755 "$rule_script"

cat > "$service_file" <<SERVICE
[Unit]
Description=Restrict RHospital statistics API to ccnode
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=${rule_script}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now rhospital-statistics-firewall.service
systemctl is-enabled rhospital-statistics-firewall.service
systemctl is-active rhospital-statistics-firewall.service
