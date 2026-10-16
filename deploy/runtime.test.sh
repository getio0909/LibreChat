#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
project="librechat-studio-smoke-$RANDOM-$RANDOM"
compose=(docker compose -p "$project" -f "$work/compose.yml" --env-file "$work/.env")

cleanup() {
  status=$?
  trap - EXIT
  if (( status != 0 )); then
    "${compose[@]}" logs --tail 50 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf -- "$work"
  exit "$status"
}
trap cleanup EXIT

cp "$source_dir/compose.yml" "$work/compose.yml"
printf 'LIBRECHAT_IMAGE=unused\nSTUDIO_IMAGE=%s\nMEILI_MASTER_KEY=fixture\nRAG_DB_PASSWORD=fixture\n' \
  "${STUDIO_IMAGE:?Set STUDIO_IMAGE to the built studio image}" > "$work/.env"

shared_token=$(printf 'a%.0s' {1..64})
file_signing_key=$(printf 'b%.0s' {1..64})
cat > "$work/studio.json" <<EOF
{
  "shared_token": "$shared_token",
  "file_signing_key": "$file_signing_key",
  "public_base_url": "https://dev.xuqian.io",
  "gemini_api_key": "dummy-gemini-key",
  "ark_api_key": "dummy-ark-key",
  "seedream_model": "doubao-seedream-5-0-pro-260628",
  "seed3d_model": "doubao-seed3d-2-0-260328",
  "planner_model": "gemini-4-argon",
  "planner_thinking_level": "HIGH",
  "max_parallel_per_user": 3,
  "seed3d_max_parallel": 5,
  "seedream_timeout_seconds": 240,
  "planner_timeout_seconds": 540
}
EOF
chmod 600 "$work/studio.json"
if [[ $(id -u) == 0 ]]; then
  chown 10001:10001 "$work/studio.json"
elif sudo -n true 2>/dev/null; then
  sudo chown 10001:10001 "$work/studio.json"
fi

# 启动加固后的 studio 容器并等待健康检查通过。
"${compose[@]}" up -d --wait --wait-timeout 60 studio

# 在容器内部验证非特权用户、/health 端点、鉴权拒绝与 MCP tools/list（不触发任何付费调用）。
"${compose[@]}" exec -T -e "STUDIO_SHARED_TOKEN=$shared_token" studio python - <<'PY'
import json
import os
import urllib.error
import urllib.request

assert os.getuid() == 10001, f"Expected UID 10001, got {os.getuid()}"

with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5) as resp:
    assert resp.status == 200, f"Unexpected /health status: {resp.status}"
    health = resp.read().decode("utf-8").strip()
    assert health == "OK", f"Unexpected /health body: {health!r}"

token = os.environ["STUDIO_SHARED_TOKEN"]
valid_user_id = "0123456789abcdef01234567"
init_probe_bytes = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode("utf-8")

# 验证缺少共享令牌、错误共享令牌、以及非法格式的用户 ID 均返回 HTTP 403。
for desc, bad_headers in [
    (
        "missing token",
        {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-LibreChat-User-ID": valid_user_id,
        },
    ),
    (
        "wrong token",
        {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Studio-Token": "wrong-studio-token",
            "X-LibreChat-User-ID": valid_user_id,
        },
    ),
    (
        "malformed user id",
        {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Studio-Token": token,
            "X-LibreChat-User-ID": "smoke-user-1",
        },
    ),
]:
    bad_req = urllib.request.Request(
        "http://127.0.0.1:8000/mcp",
        data=init_probe_bytes,
        headers=bad_headers,
        method="POST",
    )
    try:
        urllib.request.urlopen(bad_req, timeout=5)
        raise AssertionError(f"Expected MCP request with {desc} to be rejected with 403")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403, f"Expected 403 for {desc}, got {exc.code}"

base_headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "X-Studio-Token": token,
    "X-LibreChat-User-ID": valid_user_id,
    "X-LibreChat-Conversation-ID": "smoke-conv-1",
}

def parse_mcp_payload(raw_bytes: bytes) -> dict:
    text = raw_bytes.decode("utf-8").strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            data_str = line[len("data:"):].strip()
            if data_str:
                return json.loads(data_str)
    raise AssertionError(f"Unable to parse MCP response payload: {text!r}")

init_body = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "studio-smoke-test", "version": "1.0.0"},
    },
}
init_req = urllib.request.Request(
    "http://127.0.0.1:8000/mcp",
    data=json.dumps(init_body).encode("utf-8"),
    headers=base_headers,
    method="POST",
)
with urllib.request.urlopen(init_req, timeout=10) as resp:
    session_id = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
    init_payload = parse_mcp_payload(resp.read())
    assert "result" in init_payload, f"Initialize failed: {init_payload}"

headers_with_session = dict(base_headers)
if session_id:
    headers_with_session["Mcp-Session-Id"] = session_id

notify_req = urllib.request.Request(
    "http://127.0.0.1:8000/mcp",
    data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode("utf-8"),
    headers=headers_with_session,
    method="POST",
)
try:
    with urllib.request.urlopen(notify_req, timeout=10) as _:
        pass
except urllib.error.HTTPError as exc:
    if exc.code not in (200, 202, 204):
        raise

list_req = urllib.request.Request(
    "http://127.0.0.1:8000/mcp",
    data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).encode("utf-8"),
    headers=headers_with_session,
    method="POST",
)
with urllib.request.urlopen(list_req, timeout=10) as resp:
    tools_payload = parse_mcp_payload(resp.read())

tools = tools_payload.get("result", {}).get("tools", [])
tool_names = {tool["name"] for tool in tools}
expected_tools = {
    "generate_image",
    "edit_image",
    "design_concepts",
    "list_images",
    "search_library",
    "compare_materials",
    "create_3d_model",
    "check_3d_model",
}
assert expected_tools.issubset(tool_names), f"Missing expected tools: {expected_tools - tool_names} (got {tool_names})"
assert os.path.isdir("/kb/records"), "Expected /kb/records directory inside studio container"
assert os.path.isfile("/kb/catalog/materials.yaml"), "Expected /kb/catalog/materials.yaml inside studio container"
print(f"Studio smoke test passed with tools: {sorted(tool_names)}")
PY
