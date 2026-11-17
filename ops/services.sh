#!/usr/bin/env bash
set -euo pipefail
MEMU_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
action="${1:-status}"
role="${2:-all}"
if [[ "$action" == install ]]; then
  shift
  exec python3 "$MEMU_PROJECT_ROOT/ops/install_services.py" "$@"
fi
case "$role" in
  core|mcp) units=("memu-$role.service") ;;
  all) units=(memu-core.service memu-mcp.service) ;;
  *) echo 'Role must be core, mcp, or all' >&2; exit 2 ;;
esac
case "$action" in
  start|stop|restart) exec systemctl --user "$action" "${units[@]}" ;;
  status) exec systemctl --user status --no-pager "${units[@]}" ;;
  *) echo 'Usage: bash ops/services.sh {install|start|stop|restart|status} [core|mcp|all]' >&2; exit 2 ;;
esac
