#!/usr/bin/env bash
set -euo pipefail

app_dir="${1:-/home/ubuntu/rhospital-statistics}"
image_name="${2:-hospital-ops-dashboard}"
image_tag="${3:?image tag is required}"
image_tar="${4:?image tar path is required}"
compose_source="${5:?compose source path is required}"

cd "$app_dir"
test -f .env
mkdir -p data
docker load -i "$image_tar"
cp "$compose_source" docker-compose.yml

export DASHBOARD_IMAGE="${image_name}:${image_tag}"
docker compose up -d --no-build --remove-orphans

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:18092/healthz >/dev/null; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:18092/healthz >/dev/null

docker image ls "$image_name" --format '{{.Repository}}:{{.Tag}}' \
  | grep -v -E ":(${image_tag}|latest)$" \
  | xargs -r docker image rm || true
docker image prune -f >/dev/null
rm -f "$image_tar"
docker compose ps
