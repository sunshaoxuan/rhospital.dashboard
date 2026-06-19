#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

detect_bind_ip() {
  ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}' || true
}

is_real_ip() {
  value="${1:-}"
  [ -n "$value" ] || return 1
  case "$value" in
    localhost|0.0.0.0|127.*|::|::1) return 1 ;;
  esac
  return 0
}

is_configured_value() {
  value="${1:-}"
  [ -n "$value" ] || return 1
  case "$value" in
    *\<*\>*|replace_me|changeme) return 1 ;;
  esac
  return 0
}

get_env_value() {
  key="$1"
  grep "^${key}=" .env 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

set_env_value() {
  key="$1"
  value="$2"
  file=".env"
  if [ -f "$file" ] && grep -q "^${key}=" "$file"; then
    tmp="${file}.tmp"
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {$0=k"="v} {print}' "$file" > "$tmp"
    mv "$tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

if [ -d ".git" ] && [ "${SKIP_GIT_PULL:-0}" != "1" ]; then
  git pull --ff-only
fi

detected_ip="$(detect_bind_ip)"

if [ ! -f ".env" ]; then
  echo "首次安装，需要创建本机 .env。"
  printf "看板访问 IP，不能是 localhost/127/0.0.0.0 [%s]: " "$detected_ip"
  read -r bind_ip
  bind_ip="${bind_ip:-$detected_ip}"
  if ! is_real_ip "$bind_ip"; then
    echo "DASHBOARD_PUBLIC_IP 必须是真实 IP，不能是 localhost/127/0.0.0.0" >&2
    exit 1
  fi

  printf "生产只读 PostgreSQL URL，例如 postgresql://1.2.3.4:5432/hospital: "
  read -r prod_url
  printf "生产只读数据库用户名: "
  read -r prod_user
  printf "生产只读数据库密码: "
  stty -echo
  read -r prod_password
  stty echo
  printf '\n'

  cat > .env <<EOF
DASHBOARD_PUBLIC_IP=$bind_ip
DASHBOARD_PUBLIC_PORT=18091
PROD_DB_URL=$prod_url
PROD_DB_USERNAME=$prod_user
PROD_DB_PASSWORD=$prod_password
OPS_DASHBOARD_TIME_ZONE=Asia/Tokyo
OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS=10
EOF
else
  bind_ip="$(get_env_value DASHBOARD_PUBLIC_IP)"
  if ! is_real_ip "$bind_ip"; then
    printf "DASHBOARD_PUBLIC_IP 缺失或无效，请输入真实访问 IP [%s]: " "$detected_ip"
    read -r bind_ip
    bind_ip="${bind_ip:-$detected_ip}"
    if ! is_real_ip "$bind_ip"; then
      echo "DASHBOARD_PUBLIC_IP 必须是真实 IP，不能是 localhost/127/0.0.0.0" >&2
      exit 1
    fi
    set_env_value "DASHBOARD_PUBLIC_IP" "$bind_ip"
  fi
  prod_url="$(get_env_value PROD_DB_URL)"
  if ! is_configured_value "$prod_url"; then
    printf "生产只读 PostgreSQL URL，例如 postgresql://1.2.3.4:5432/hospital: "
    read -r prod_url
    set_env_value "PROD_DB_URL" "$prod_url"
  fi
  prod_user="$(get_env_value PROD_DB_USERNAME)"
  if ! is_configured_value "$prod_user"; then
    printf "生产只读数据库用户名: "
    read -r prod_user
    set_env_value "PROD_DB_USERNAME" "$prod_user"
  fi
  prod_password="$(get_env_value PROD_DB_PASSWORD)"
  if ! is_configured_value "$prod_password"; then
    printf "生产只读数据库密码: "
    stty -echo
    read -r prod_password
    stty echo
    printf '\n'
    set_env_value "PROD_DB_PASSWORD" "$prod_password"
  fi
  if ! grep -q '^OPS_DASHBOARD_TIME_ZONE=' .env; then
    set_env_value "OPS_DASHBOARD_TIME_ZONE" "Asia/Tokyo"
  fi
  if ! grep -q '^OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS=' .env; then
    set_env_value "OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS" "10"
  fi
  if ! grep -q '^DASHBOARD_PUBLIC_PORT=' .env; then
    set_env_value "DASHBOARD_PUBLIC_PORT" "18091"
  fi
fi

docker compose up -d --build

echo
echo "本地运营看板已启动或升级:"
public_port="$(get_env_value DASHBOARD_PUBLIC_PORT)"
echo "http://$bind_ip:$public_port/"
echo
docker compose ps
