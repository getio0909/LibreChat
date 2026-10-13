#!/usr/bin/env bash
set -euo pipefail

export FREECAD_MCP_SOCKET=/tmp/freecad_mcp.sock
"$FREECAD_MCP_FREECAD_BIN" /opt/freecad-mcp/AICopilot/headless_server.py > /tmp/freecad.log 2>&1 &
cad_pid=$!
cleanup() {
  kill "$cad_pid" 2>/dev/null || true
  wait "$cad_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
for _ in {1..120}; do
  if [[ -S "$FREECAD_MCP_SOCKET" ]]; then
    break
  fi
  if ! kill -0 "$cad_pid" 2>/dev/null; then
    echo 'The FreeCAD worker exited before becoming ready.' >&2
    exit 1
  fi
  sleep 0.5
done
if [[ ! -S "$FREECAD_MCP_SOCKET" ]]; then
  echo 'The FreeCAD worker did not become ready.' >&2
  exit 1
fi
node /opt/gateway/node_modules/supergateway/dist/index.js "$@"
