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
      for file in "${config_files[@]}" .file_signing_key; do
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
  fi
  if ! jq -e --arg role "$expected_role" '.user.role == $role' "$stage/login.json" >/dev/null; then
    printf 'Default %s account role verification failed.\n' "$account" >&2
    exit 1
  fi
done
# 账户和角色验证通过后恢复用户配置的注册设置。
"${compose[@]}" up -d --remove-orphans --wait --wait-timeout 240
rm -f -- "$gate"
echo 'Development deployment passed health checks.'
