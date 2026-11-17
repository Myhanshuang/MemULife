#!/usr/bin/env bash
set -euo pipefail
MEMU_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$MEMU_PROJECT_ROOT"
case "${1:-}" in
  core) MEMU_DEFAULT_PYTHON="$MEMU_PROJECT_ROOT/.venv-embedding/bin/python" ;;
  *) MEMU_DEFAULT_PYTHON="$MEMU_PROJECT_ROOT/.venv/bin/python" ;;
esac
exec "${MEMU_PYTHON:-$MEMU_DEFAULT_PYTHON}" -m memu "$@"
