#!/usr/bin/env bash
set -euo pipefail
MEMU_TAILNET_PORT="${MEMU_TAILNET_PORT:-15072}"
if [[ ! "$MEMU_TAILNET_PORT" =~ ^[1-9][0-9]{3,4}$ ]] || ((MEMU_TAILNET_PORT < 1024 || MEMU_TAILNET_PORT > 65535)); then
  echo 'MEMU_TAILNET_PORT must be a TCP port from 1024 to 65535.' >&2
  exit 2
fi
case "${1:-status}" in
  start) exec tailscale serve --bg --tcp="$MEMU_TAILNET_PORT" tcp://127.0.0.1:5072 ;;
  stop) exec tailscale serve --tcp="$MEMU_TAILNET_PORT" off ;;
  status) exec tailscale serve status ;;
  *) echo 'Usage: bash tailscale-serve.sh {start|stop|status}' >&2; exit 2 ;;
esac
