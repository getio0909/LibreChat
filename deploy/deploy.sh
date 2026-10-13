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
config_files=(compose.yml librechat.yaml .env image.env design-admin.json design-user.json files-admin.yaml files-user.yaml)
gate=/etc/nginx/librechat-dev.provisioning
compose=(docker compose -p librechat-dev --project-directory "$deploy_root" \
  --env-file "$deploy_root/.env" --env-file "$deploy_root/image.env" -f "$deploy_root/compose.yml")
own_service_configs() {
  for file in design-admin.json design-user.json files-admin.yaml files-user.yaml; do
    if [[ -f "$deploy_root/$file" ]]; then
      chown 10001:10001 "$deploy_root/$file" || return 1
    fi
  done
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
      # 单文件挂载保留旧 inode；强制重建才能恢复已轮换的密钥和文件服务凭据。
      if "$restored" && own_service_configs && "${compose[@]}" up -d --remove-orphans --force-recreate --wait --wait-timeout 240; then
        rm -f -- "$gate"
        echo 'The previous application deployment has been restored.' >&2
      else
        echo 'Restoring the previous application deployment failed.' >&2
      fi
    elif "${compose[@]}" down; then
      for file in "${config_files[@]}"; do rm -f -- "$deploy_root/$file"; done
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
    ./|./compose.yml|./librechat.yaml|./.env|./image.env|./registry.token|./accounts.json|./design-admin.json|./design-user.json|./files-admin.yaml|./files-user.yaml) ;;
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
if ! grep -Eq '^LIBRECHAT_IMAGE=ghcr\.io/getio0909/librechat-dev@sha256:[0-9a-f]{64}$' "$stage/image.env"; then
  echo 'The deployment must reference an immutable development image digest.' >&2
  exit 1
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
  mkdir -p "$deploy_root/.previous"
  for file in "${config_files[@]}"; do
    if [[ -f "$deploy_root/$file" ]]; then
      cp "$deploy_root/$file" "$deploy_root/.previous/$file"
    else
      rm -f -- "$deploy_root/.previous/$file"
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
own_service_configs

# 默认账户验证完成前关闭公开注册，避免首个管理员被抢先创建。
cat > "$stage/accounts.yml" <<'EOF'
services:
  api:
    environment:
      ALLOW_REGISTRATION: 'false'
EOF
closed_compose=("${compose[@]}" -f "$stage/accounts.yml")
if ! "${closed_compose[@]}" up -d --remove-orphans --wait --wait-timeout 240; then
  echo 'Deployment health checks failed.' >&2
  exit 1
fi
"${closed_compose[@]}" ps

# 验证真实协议握手和工具发现，避免代理正常但 CAD 服务实际不可用。
cat > "$stage/checks.js" <<'NODE'
const { Client } = require('@modelcontextprotocol/sdk/client/index.js');
const { SSEClientTransport } = require('@modelcontextprotocol/sdk/client/sse.js');
const { StreamableHTTPClientTransport } = require('@modelcontextprotocol/sdk/client/streamableHttp.js');
(async () => {
  const accounts = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
  for (const account of ['admin', 'user']) {
   const id = process.env[`CAD_${account.toUpperCase()}_USER_ID`];
   for (const [name, transport] of [
    [`FreeCAD ${account}`, new SSEClientTransport(new URL(`http://freecad-${account}:8000/sse`))],
    [`Blender ${account}`, new StreamableHTTPClientTransport(new URL(`http://blender-${account}:10101/`))],
    [`Design ${account}`, new StreamableHTTPClientTransport(new URL(`http://design-${account}:8000/mcp`), { requestInit: { headers: { 'X-LibreChat-User-ID': id } } })],
   ]) {
    const client = new Client({ name: 'deployment-check', version: '1.0.0' });
    try {
      await client.connect(transport, { timeout: 120000 });
      const result = await client.listTools();
      if (result.tools.length === 0) throw new Error('The CAD server returned no tools.');
      console.log(`${name} MCP is ready with ${result.tools.length} tools.`);
    } finally {
      await client.close();
    }
   }
   const url = `http://files-${account}:5000/cad-files/${account}/?json`;
   const credential = accounts[account];
   const wrong = accounts[account === 'admin' ? 'user' : 'admin'];
   const authorization = (value) => 'Basic ' + Buffer.from(`${value.username}:${value.password}`).toString('base64');
   for (const [headers, status] of [
     [{ Authorization: authorization(credential) }, 200], [{}, 401],
     [{ Authorization: authorization(wrong) }, 401],
   ]) {
     const response = await fetch(url, { headers, signal: AbortSignal.timeout(10000) });
     await response.body?.cancel();
     if (response.status !== status) throw new Error('CAD file authentication verification failed.');
   }
   console.log(`Private CAD file access verified for ${account}.`);
  }
})().catch(() => { console.error('CAD MCP readiness verification failed.'); process.exit(1); });
NODE

# 按顺序创建首个管理员和普通用户，密码通过标准输入传递给现有账户工具。
printf '{}\n' > "$stage/owners.json"
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
  user_id=$(jq -r '.user.id // .user._id' "$stage/login.json")
  if [[ ! "$user_id" =~ ^[0-9a-f]{24}$ ]]; then
    echo 'The verified account has no valid user ID.' >&2
    exit 1
  fi
  printf 'CAD_%s_USER_ID=%s\n' "${account^^}" "$user_id" >> "$deploy_root/.env"
  jq --arg account "$account" --arg id "$user_id" '.[$account] = $id' \
    "${stage}/owners.json" > "$stage/owners.next.json"
  mv "$stage/owners.next.json" "$stage/owners.json"
  jq --arg id "$user_id" '.userId = $id' "$stage/design-$account.json" > "$stage/design-bound.json"
  install -m 600 "$stage/design-bound.json" "$deploy_root/design-$account.json"
done
own_service_configs
# 在公开访问恢复前，将内部网络地址绑定到已验证的用户标识。
"${closed_compose[@]}" exec -T api node -e '
const fs = require("node:fs"), yaml = require("js-yaml");
const owners = JSON.parse(fs.readFileSync(0, "utf8"));
const config = yaml.load(fs.readFileSync("/app/librechat.yaml", "utf8"));
config.mcpSettings.allowedAddresses = Object.values(owners).flatMap(id => ["freecad-" + id + ":8000", "blender-" + id + ":10101", "design-" + id + ":8000"]);
process.stdout.write(yaml.dump(config));
' \
  < "$stage/owners.json" > "$stage/configured.yaml"
install -m 600 "$stage/configured.yaml" "$deploy_root/librechat.yaml"
"${closed_compose[@]}" up -d --remove-orphans --wait --wait-timeout 240
"${closed_compose[@]}" up -d --no-deps --force-recreate --wait --wait-timeout 240 files-admin files-user design-admin design-user api
"${closed_compose[@]}" exec -T api node -e "$(cat "$stage/checks.js")" < "$stage/accounts.json"
# 同时验证 Nginx 使用的主机端口，容器内部可达并不代表端口已发布。
for account in admin user; do
  port=5001
  if [[ "$account" == user ]]; then port=5002; fi
  jq -r --arg account "$account" '"Authorization: Basic " + (.[$account] | (.username + ":" + .password) | @base64)' \
    "$stage/accounts.json" > "$stage/files-header"
  curl --fail --silent --show-error -H "@$stage/files-header" -o /dev/null \
    "http://127.0.0.1:$port/cad-files/$account/?json"
done
# 账户和角色验证通过后恢复用户配置的注册设置。
"${compose[@]}" up -d --remove-orphans --wait --wait-timeout 240
rm -f -- "$gate"
echo 'Development deployment passed health checks.'
