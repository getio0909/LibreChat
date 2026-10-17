#!/usr/bin/env bash
set -euo pipefail

deploy_root="${1:?Supply the deployment directory}"
if [[ "${SSH_ORIGINAL_COMMAND:-}" != "deploy $deploy_root" ]]; then
  echo 'Only the configured development deployment command is permitted.' >&2
  exit 1
fi

umask 077
mkdir -p "$deploy_root"
exec 9>"$deploy_root/.deploy.lock"
flock -w 300 9
stage=$(mktemp -d "$deploy_root/.incoming.XXXXXX")
had_previous=false
activated=false
gate=/etc/nginx/librechat-dev.provisioning
config_files=(compose.yml librechat.yaml .env image.env studio.json)
compose=(docker compose -p librechat-dev --project-directory "$deploy_root" \
  --env-file "$deploy_root/.env" --env-file "$deploy_root/image.env" -f "$deploy_root/compose.yml")

# 将 studio.json 归属权设为非特权容器用户 10001:10001，保持 0600 权限。
own_service_configs() {
  if [[ -f "$deploy_root/studio.json" ]]; then
    chown 10001:10001 "$deploy_root/studio.json"
  fi
}

cleanup() {
  status=$?
  trap - EXIT
  if (( status != 0 )) && "$activated"; then
    if "$had_previous"; then
      restored=true
      for file in "${config_files[@]}"; do
        if [[ ! -f "$deploy_root/.previous/$file" ]]; then
          rm -f -- "$deploy_root/$file"
        elif ! cp "$deploy_root/.previous/$file" "$deploy_root/$file"; then
          restored=false
        fi
      done
      # 单文件挂载保留旧 inode，强制重建容器以恢复上一版本的配置文件与密钥。
      if "$restored" && own_service_configs && "${compose[@]}" up -d --remove-orphans --force-recreate --wait --wait-timeout 240; then
        rm -f -- "$gate"
        echo 'The previous application deployment has been restored.' >&2
      else
        echo 'Restoring the previous application deployment failed.' >&2
      fi
    elif "${compose[@]}" down --remove-orphans; then
      for file in "${config_files[@]}" .file_signing_key .studio_agent_ids.json; do
        rm -f -- "$deploy_root/$file"
      done
      echo 'The failed initial application deployment has been stopped.' >&2
    else
      echo 'Stopping the failed initial application deployment failed.' >&2
    fi
  fi
  rm -rf -- "$stage"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
cat > "$stage/bundle.tgz"

# 在解包之前限制归档成员，防止部署凭据被用于写入其他路径。
while IFS= read -r member; do
  case "$member" in
    ./|./compose.yml|./librechat.yaml|./.env|./image.env|./registry.token|./accounts.json|./studio.json) ;;
    *) echo 'Unexpected deployment archive member.' >&2; exit 1 ;;
  esac
done < <(tar -tzf "$stage/bundle.tgz")
tar -xzf "$stage/bundle.tgz" -C "$stage" --no-same-owner --no-same-permissions
for file in "${config_files[@]}" registry.token accounts.json; do
  if [[ ! -f "$stage/$file" || -L "$stage/$file" ]]; then
    echo 'A required deployment file is missing or is not a regular file.' >&2
    exit 1
  fi
done
if ! grep -Eq '^LIBRECHAT_IMAGE=ghcr\.io/getio0909/librechat-dev(:[A-Za-z0-9_.-]+)?@sha256:[0-9a-f]{64}$' "$stage/image.env"; then
  echo 'The deployment must reference an immutable development image digest.' >&2
  exit 1
fi
if ! grep -Eq '^STUDIO_IMAGE=ghcr\.io/getio0909/librechat-studio(:[A-Za-z0-9_.-]+)?@sha256:[0-9a-f]{64}$' "$stage/image.env"; then
  echo 'The deployment must reference an immutable studio image digest.' >&2
  exit 1
fi
if ! jq -e '
  (.shared_token | type == "string" and length >= 64)
  and (.file_signing_key | type == "string" and test("^[0-9a-fA-F]{64}$"))
  and (.gemini_api_key | type == "string" and length >= 1)
  and ((.ark_api_key // "") | type == "string")
' "$stage/studio.json" >/dev/null; then
  echo 'Studio configuration settings are invalid.' >&2
  exit 1
fi
# 复用服务器上已持久化的文件签名密钥；仅在首次部署且密钥不存在时生成并持久化。
persisted_signing_key=''
if [[ -f "$deploy_root/.file_signing_key" ]]; then
  candidate_key=$(tr -d '\r\n' < "$deploy_root/.file_signing_key")
  if [[ "$candidate_key" =~ ^[0-9a-fA-F]{64}$ ]]; then
    persisted_signing_key=$candidate_key
  fi
fi
if [[ -z "$persisted_signing_key" && -f "$deploy_root/studio.json" ]]; then
  candidate_key=$(jq -r '.file_signing_key // empty' "$deploy_root/studio.json" 2>/dev/null || true)
  if [[ "$candidate_key" =~ ^[0-9a-fA-F]{64}$ ]]; then
    persisted_signing_key=$candidate_key
  fi
fi
if [[ -n "$persisted_signing_key" ]]; then
  jq --arg key "$persisted_signing_key" '.file_signing_key = $key' "$stage/studio.json" > "$stage/studio.json.tmp"
  mv "$stage/studio.json.tmp" "$stage/studio.json"
else
  persisted_signing_key=$(jq -r '.file_signing_key' "$stage/studio.json")
fi
if ! jq -e '
  [.admin, .user] | all(.
    | (.email | type == "string" and contains("@"))
    and (.username | type == "string" and length >= 3)
    and (.password | type == "string" and length >= 8))
' "$stage/accounts.json" >/dev/null; then
  echo 'Default account settings are invalid.' >&2
  exit 1
fi

# 拉取镜像所需的短期令牌只保存在本次部署的临时目录中。
export DOCKER_CONFIG="$stage/docker"
docker login ghcr.io -u getio0909 --password-stdin < "$stage/registry.token"
docker compose -p librechat-dev --project-directory "$stage" --env-file "$stage/.env" \
  --env-file "$stage/image.env" -f "$stage/compose.yml" config --quiet
docker compose -p librechat-dev --project-directory "$stage" --env-file "$stage/.env" \
  --env-file "$stage/image.env" -f "$stage/compose.yml" pull

if [[ -f "$deploy_root/image.env" ]]; then
  rm -rf -- "$deploy_root/.previous"
  mkdir -p "$deploy_root/.previous"
  for file in "${config_files[@]}"; do
    if [[ -f "$deploy_root/$file" ]]; then
      cp "$deploy_root/$file" "$deploy_root/.previous/$file"
    fi
  done
  had_previous=true
fi
activated=true
# 从代理入口隔离所有登录方式，首个管理员验证通过后才恢复公开访问。
: > "$gate"
for file in "${config_files[@]}"; do
  install -m 600 "$stage/$file" "$deploy_root/$file"
done
printf '%s\n' "$persisted_signing_key" > "$deploy_root/.file_signing_key"
chmod 600 "$deploy_root/.file_signing_key"
own_service_configs

# 默认账户验证完成前关闭公开注册，避免首个管理员被抢先创建。
cat > "$stage/accounts.yml" <<'EOF'
services:
  api:
    environment:
      ALLOW_REGISTRATION: 'false'
EOF
closed_compose=("${compose[@]}" -f "$stage/accounts.yml")
if ! "${closed_compose[@]}" up -d --remove-orphans --force-recreate --wait --wait-timeout 240; then
  echo 'Deployment health checks failed.' >&2
  exit 1
fi
"${closed_compose[@]}" ps

# 按顺序创建首个管理员和普通用户，密码通过标准输入传递给现有账户工具。
admin_token=''
for account in admin user; do
  email=$(jq -r --arg account "$account" '.[$account].email' "$stage/accounts.json")
  username=$(jq -r --arg account "$account" '.[$account].username' "$stage/accounts.json")
  password=$(jq -r --arg account "$account" '.[$account].password' "$stage/accounts.json")
  if output=$(printf '%s\n' "$password" | "${closed_compose[@]}" exec -T api \
    node /app/config/create-user.js "$email" "$username" "$username" --email-verified=true 2>&1); then
    printf 'Default %s account created.\n' "$account"
  elif [[ "$output" == *'A user with that email or username already exists!'* ]]; then
    printf 'Default %s account already exists.\n' "$account"
  else
    printf 'Default %s account provisioning failed.\n' "$account" >&2
    exit 1
  fi
  jq -c --arg account "$account" '{email: .[$account].email, password: .[$account].password}' \
    "$stage/accounts.json" | curl --fail --silent --show-error \
      -H 'Content-Type: application/json' --data-binary @- \
      -o "$stage/login.json" http://127.0.0.1:3080/api/auth/login
  expected_role=USER
  if [[ "$account" == admin ]]; then
    expected_role=ADMIN
    admin_token=$(jq -r '.token // empty' "$stage/login.json")
  fi
  if ! jq -e --arg role "$expected_role" '.user.role == $role' "$stage/login.json" >/dev/null; then
    printf 'Default %s account role verification failed.\n' "$account" >&2
    exit 1
  fi
done

# 幂等创建或更新 Studio 持久化智能体（挂载 gemini_image_gen 与 zyb_studio MCP 工具），授权普通用户只读使用，并将生成的 agent_id 注入 librechat.yaml。
if grep -qE 'agent___STUDIO_AGENT_ID_(MAIN|FAST)__' "$stage/librechat.yaml"; then
  if [[ -z "$admin_token" ]]; then
    echo 'Admin token missing for Studio agent provisioning.' >&2
    exit 1
  fi
  design_instructions=$(awk '
    /promptPrefix: &designPromptPrefix \|-/ { in_block=1; next }
    in_block {
      if ($0 ~ /^          /) {
        sub(/^          /, "")
        lines = (lines == "" ? $0 : lines "\n" $0)
      } else if ($0 ~ /^[[:space:]]*$/) {
        lines = lines "\n"
      } else {
        exit
      }
    }
    END { printf "%s", lines }
  ' "$stage/librechat.yaml")
  if [[ -z "$design_instructions" ]]; then
    echo 'Failed to extract Studio design instructions from librechat.yaml.' >&2
    exit 1
  fi

  printf '%s' '{"USE":true,"CREATE":false,"SHARE":false,"SHARE_PUBLIC":false}' | \
    curl --fail --silent --show-error -X PUT \
      -H "Authorization: Bearer $admin_token" \
      -H 'Content-Type: application/json' \
      --data-binary @- \
      -o "$stage/role-agents.json" http://127.0.0.1:3080/api/roles/USER/agents

  if [[ -f "$deploy_root/.studio_agent_ids.json" ]] && jq -e 'type == "object"' "$deploy_root/.studio_agent_ids.json" >/dev/null 2>&1; then
    cp "$deploy_root/.studio_agent_ids.json" "$stage/studio-agent-ids.json"
  else
    printf '{}\n' > "$stage/studio-agent-ids.json"
  fi

  # 分页拉取全部可见智能体列表，避免超过单页 100 条上限时漏检既有 Studio 智能体并产生重复创建。
  printf '{"data":[]}\n' > "$stage/agents-list.json"
  agents_cursor=''
  declare -A seen_agent_cursors=()
  for (( page_idx = 0; page_idx < 50; page_idx++ )); do
    agents_url='http://127.0.0.1:3080/api/agents?limit=100'
    if [[ -n "$agents_cursor" ]]; then
      agents_url="${agents_url}&cursor=${agents_cursor}"
    fi
    curl --fail --silent --show-error \
      -H "Authorization: Bearer $admin_token" \
      -o "$stage/agents-page.json" "$agents_url"
    jq -e '(.data | type == "array") and (.has_more | type == "boolean")' \
      "$stage/agents-page.json" >/dev/null || {
      echo 'Studio agent listing returned an invalid page.' >&2
      exit 1
    }
    jq -s '{data: ((.[0].data // []) + (.[1].data // []))}' \
      "$stage/agents-list.json" "$stage/agents-page.json" > "$stage/agents-list.json.tmp"
    mv "$stage/agents-list.json.tmp" "$stage/agents-list.json"
    has_more=$(jq -r 'if .has_more == true then "true" else "false" end' "$stage/agents-page.json")
    next_cursor=$(jq -r '.after // empty' "$stage/agents-page.json")
    if [[ "$has_more" != "true" ]]; then
      break
    fi
    if [[ -z "$next_cursor" || -n "${seen_agent_cursors[$next_cursor]:-}" ]]; then
      echo 'Studio agent listing returned incomplete pagination.' >&2
      exit 1
    fi
    seen_agent_cursors[$next_cursor]=1
    agents_cursor=$next_cursor
  done
  if [[ "$has_more" == "true" ]]; then
    echo 'Studio agent listing exceeded the pagination limit.' >&2
    exit 1
  fi

  is_studio_owned_agent_file() {
    local candidate_file=$1
    local owner_marker=$2
    jq -e --arg marker "$owner_marker" '
      (.id | type == "string" and test("^agent_[A-Za-z0-9_-]+$"))
      and (.instructions | type == "string" and endswith($marker))
    ' "$candidate_file" >/dev/null 2>&1
  }

  resolve_existing_studio_agent_id() {
    local placeholder=$1
    local owner_marker=$2
    local persisted_id
    persisted_id=$(jq -r --arg k "$placeholder" '.[$k] // empty' "$stage/studio-agent-ids.json")
    if [[ "$persisted_id" =~ ^agent_[A-Za-z0-9_-]+$ ]]; then
      if jq -e --arg id "$persisted_id" '(.data // []) | map(select(.id == $id)) | length > 0' "$stage/agents-list.json" >/dev/null 2>&1; then
        curl --fail --silent --show-error \
          -H "Authorization: Bearer $admin_token" \
          -o "$stage/agent-expanded.json" "http://127.0.0.1:3080/api/agents/$persisted_id/expanded" || return 1
        if is_studio_owned_agent_file "$stage/agent-expanded.json" "$owner_marker"; then
          printf '%s' "$persisted_id"
          return 0
        fi
        echo 'Persisted Studio agent ID has no matching ownership marker.' >&2
        return 1
      fi
    fi

    local candidate_id matched_id=''
    while IFS= read -r candidate_id; do
      [[ -z "$candidate_id" ]] && continue
      curl --fail --silent --show-error \
        -H "Authorization: Bearer $admin_token" \
        -o "$stage/agent-expanded.json" "http://127.0.0.1:3080/api/agents/$candidate_id/expanded" || return 1
      if is_studio_owned_agent_file "$stage/agent-expanded.json" "$owner_marker"; then
        if [[ -n "$matched_id" ]]; then
          echo 'Multiple Studio agents carry the same ownership marker.' >&2
          return 1
        fi
        matched_id=$candidate_id
      fi
    done < <(jq -r '(.data // []) | .[].id // empty' "$stage/agents-list.json")
    printf '%s' "$matched_id"
  }

  provision_studio_agent() {
    local placeholder=$1
    local payload_file=$2
    local owner_marker=$3
    local existing_id
    existing_id=$(resolve_existing_studio_agent_id "$placeholder" "$owner_marker")
    if [[ -n "$existing_id" ]]; then
      curl --fail --silent --show-error -X PATCH \
        -H "Authorization: Bearer $admin_token" \
        -H 'Content-Type: application/json' \
        --data-binary @"$payload_file" \
        -o "$stage/agent-saved.json" "http://127.0.0.1:3080/api/agents/$existing_id"
    else
      curl --fail --silent --show-error -X POST \
        -H "Authorization: Bearer $admin_token" \
        -H 'Content-Type: application/json' \
        --data-binary @"$payload_file" \
        -o "$stage/agent-saved.json" http://127.0.0.1:3080/api/agents
    fi
    if ! jq -e --arg marker "$owner_marker" '
      (.id | type == "string" and test("^agent_[A-Za-z0-9_-]+$"))
      and (._id | type == "string" and length >= 1)
      and (.tools | index("gemini_image_gen") != null)
      and (.tools | index("sys__all__sys_mcp_zyb_studio") != null)
      and (.instructions | endswith($marker))
    ' "$stage/agent-saved.json" >/dev/null; then
      printf 'Studio agent provisioning verification failed for %s.\n' "$agent_name" >&2
      exit 1
    fi
    local resolved_id resolved_oid
    resolved_id=$(jq -r '.id' "$stage/agent-saved.json")
    resolved_oid=$(jq -r '._id' "$stage/agent-saved.json")
    printf '%s' '{"public":true,"publicAccessRoleId":"agent_viewer","updated":[{"type":"role","id":"USER","accessRoleId":"agent_viewer"}],"removed":[]}' | \
      curl --fail --silent --show-error -X PUT \
        -H "Authorization: Bearer $admin_token" \
        -H 'Content-Type: application/json' \
        --data-binary @- \
        -o "$stage/agent-perms.json" "http://127.0.0.1:3080/api/permissions/agent/$resolved_oid"
    sed -i "s|$placeholder|$resolved_id|g" "$stage/librechat.yaml"
    jq --arg k "$placeholder" --arg v "$resolved_id" '.[$k] = $v' "$stage/studio-agent-ids.json" > "$stage/studio-agent-ids.json.tmp"
    mv "$stage/studio-agent-ids.json.tmp" "$stage/studio-agent-ids.json"
  }

  main_owner_marker='<!-- librechat-studio-owner:zyb-design:v1 -->'
  fast_owner_marker='<!-- librechat-studio-owner:zyb-design-fast:v1 -->'
  jq -n --arg instructions "$design_instructions" --arg marker "$main_owner_marker" '{
    name: "庄严设计助手",
    description: "寺院装饰、石材大板、家具与展位概念设计主力助手",
    provider: "google",
    model: "gemini-4-argon",
    instructions: ($instructions + "\n\n" + $marker),
    tools: ["gemini_image_gen", "sys__all__sys_mcp_zyb_studio"],
    model_parameters: {
      thinking: true,
      thinkingLevel: "high",
      maxContextTokens: 200000,
      maxOutputTokens: 65536
    }
  }' > "$stage/agent-main.json"
  provision_studio_agent 'agent___STUDIO_AGENT_ID_MAIN__' "$stage/agent-main.json" "$main_owner_marker"

  jq -n --arg instructions "$design_instructions" --arg marker "$fast_owner_marker" '{
    name: "庄严设计助手 · 快速",
    description: "快速响应的概念设计与图像改图助手",
    provider: "OpenRouter",
    model: "anthropic/claude-haiku-5.5",
    instructions: ($instructions + "\n\n" + $marker),
    tools: ["gemini_image_gen", "sys__all__sys_mcp_zyb_studio"],
    model_parameters: {
      maxContextTokens: 200000,
      max_tokens: 32000
    }
  }' > "$stage/agent-fast.json"
  provision_studio_agent 'agent___STUDIO_AGENT_ID_FAST__' "$stage/agent-fast.json" "$fast_owner_marker"

  if grep -qE 'agent___STUDIO_AGENT_ID_(MAIN|FAST)__' "$stage/librechat.yaml"; then
    echo 'Unresolved Studio agent placeholders remain in librechat.yaml.' >&2
    exit 1
  fi
  install -m 600 "$stage/librechat.yaml" "$deploy_root/librechat.yaml"
  install -m 600 "$stage/studio-agent-ids.json" "$deploy_root/.studio_agent_ids.json"
fi

# 账户、智能体和角色验证通过后恢复用户配置的注册设置并加载解析后的 librechat.yaml。
"${compose[@]}" up -d --remove-orphans --force-recreate --wait --wait-timeout 240
rm -f -- "$gate"
echo 'Development deployment passed health checks.'
