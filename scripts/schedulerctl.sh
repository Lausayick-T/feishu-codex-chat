#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
printf '%s\n' "提示：schedulerctl.sh 已合并到 servicectl.sh；现在会同时管理 server 和 scheduler。" >&2
exec "$SCRIPT_DIR/servicectl.sh" tmux "${1:-status}"
