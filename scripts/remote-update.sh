#!/usr/bin/env sh
set -eu

app_dir="${1:?app dir is required}"
image_name="${2:?image name is required}"
image_tag="${3:?image tag is required}"
image_tar="${4:?image tar is required}"
compose_file="${5:?compose file is required}"

cd "$app_dir"

if [ ! -f ".env" ]; then
  echo "missing $app_dir/.env" >&2
  exit 1
fi

mkdir -p data releases
cp "$compose_file" "$app_dir/docker-compose.yml"

docker load -i "$image_tar"

DASHBOARD_IMAGE="$image_name:$image_tag" docker compose up -d --no-build

if [ -x /usr/local/sbin/rhdashboard-db-firewall.sh ]; then
  /usr/local/sbin/rhdashboard-db-firewall.sh
fi

public_port="$(grep '^DASHBOARD_PUBLIC_PORT=' .env 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '\r')"
public_port="${public_port:-18091}"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${public_port}/healthz" >/dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 30 ]; then
    curl -fsS "http://127.0.0.1:${public_port}/healthz" >/dev/null
  fi
  sleep 1
done

current_image_id="$(docker compose ps -q ops-dashboard | xargs docker inspect --format '{{.Image}}')"

docker images "$image_name" --format '{{.Repository}}:{{.Tag}} {{.ID}}' | while read -r ref image_id; do
  [ -n "$ref" ] || continue
  case "$ref" in
    "$image_name:$image_tag"|"$image_name:latest"|"$image_name:local")
      continue
      ;;
  esac
  if [ "$image_id" != "$current_image_id" ]; then
    docker rmi "$ref" >/dev/null 2>&1 || true
  fi
done

docker image prune -f >/dev/null

docker compose ps
