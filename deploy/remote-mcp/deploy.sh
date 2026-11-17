#!/usr/bin/env bash
set -euo pipefail
MEMU_DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$MEMU_DEPLOY_DIR"

for config in .env mcp.env; do
  if [[ ! -f "$config" ]]; then
    echo "Missing $config; copy $config.example and configure it first." >&2
    exit 2
  fi
done

validate() {
  docker compose --profile https config --quiet
  docker compose run --rm --no-deps -T --entrypoint python \
    -v "$MEMU_DEPLOY_DIR/validate.py:/checks/validate.py:ro" mcp /checks/validate.py
}

case "${1:-}" in
  build) docker compose build mcp ;;
  validate) validate ;;
  up)
    validate
    docker compose --profile https up -d
    ;;
  up-mcp)
    validate
    docker compose up -d mcp
    ;;
  check) docker compose exec -T mcp python -m memu check ;;
  check-local)
    docker compose exec -T -e MEMU_MCP_URL=http://127.0.0.1:5071/mcp mcp python -m memu check
    ;;
  status) docker compose --profile https ps ;;
  logs) docker compose --profile https logs --tail 80 ;;
  down) docker compose --profile https down ;;
  *) echo 'Usage: bash deploy.sh {build|validate|up|up-mcp|check|check-local|status|logs|down}' >&2; exit 2 ;;
esac
