#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/bin" "$work/bundle" "$work/previous"
export PATH="$work/bin:$PATH"
export DEPLOY_TEST_LOG="$work/operations.log"
export DEPLOY_TEST_GATE="$work/provisioning"
sed "s|^gate=.*|gate='$DEPLOY_TEST_GATE'|" "$source_dir/deploy.sh" > "$work/deploy.sh"

# 保留真实的归档、文件权限和回滚逻辑，只替代会影响服务的外部命令。
cat > "$work/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == login ]]; then
  cat >/dev/null
  exit 0
fi
closed=false
root=''
while (( $# > 0 )); do
  case "$1" in
    --project-directory) root=$2; shift 2 ;;
    -f)
      if [[ "$2" == */accounts.yml ]]; then
        grep -q "ALLOW_REGISTRATION: 'false'" "$2"
        closed=true
      fi
      shift 2
      ;;
    up|exec|config|pull|ps|down) operation=$1; shift; break ;;
    *) shift ;;
  esac
done
image=$(tr '\n' ' ' < "$root/image.env")
printf '%s:%s:%s\n' "$operation" "$closed" "$image" >> "$DEPLOY_TEST_LOG"
case "$operation" in
  up)
    if [[ "$image" == *11111111* ]]; then
      test -f "$DEPLOY_TEST_GATE"
      if [[ "$DEPLOY_TEST_FAILURE" == health || ( "$DEPLOY_TEST_FAILURE" == final && "$closed" == false ) ]]; then
        exit 1
      fi
    fi
    ;;
  exec)
    "$closed"
    test -f "$DEPLOY_TEST_GATE"
    cat >/dev/null
    if [[ "$DEPLOY_TEST_FAILURE" == command ]]; then
      exit 1
    fi
    echo 'A user with that email or username already exists!' >&2
    exit 1
    ;;
esac
EOF
cat > "$work/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
method=GET
output=''
data=''
url=''
auth=''
while (( $# > 0 )); do
  case "$1" in
    -X) method=$2; shift 2 ;;
    -o) output=$2; shift 2 ;;
    -H)
      if [[ "$2" == Authorization:* ]]; then
        auth=$2
      fi
      shift 2
      ;;
    --data-binary) data=$2; shift 2 ;;
    http://*) url=$1; shift ;;
    *) shift ;;
  esac
done
body=''
if [[ "$data" == @- ]]; then
  body=$(cat)
elif [[ "$data" == @* ]]; then
  body=$(cat "${data#@}")
fi
case "$url" in
  */api/auth/login)
    email=$(jq -r .email <<< "$body")
    if [[ "$DEPLOY_TEST_FAILURE" == login ]]; then exit 22; fi
    role=USER
    if [[ "$email" == admin@example.test && "$DEPLOY_TEST_FAILURE" != role ]]; then role=ADMIN; fi
    jq -n --arg role "$role" '{token: "mock-jwt-token", user: {role: $role}}' > "$output"
    ;;
  */api/roles/USER/agents)
    test "$method" = PUT
    test "$auth" = "Authorization: Bearer mock-jwt-token"
    jq -e '.USE == true and .CREATE == false and .SHARE == false and .SHARE_PUBLIC == false' <<< "$body" >/dev/null
    printf 'api:PUT:/api/roles/USER/agents\n' >> "$DEPLOY_TEST_LOG"
    printf '%s\n' "$body" > "$output"
    ;;
  */api/agents\?limit=100)
    test "$method" = GET
    test "$auth" = "Authorization: Bearer mock-jwt-token"
    printf 'api:GET:/api/agents?limit=100\n' >> "$DEPLOY_TEST_LOG"
    if [[ -n "${DEPLOY_TEST_AGENTS_DB:-}" && -f "$DEPLOY_TEST_AGENTS_DB" ]]; then
      cp "$DEPLOY_TEST_AGENTS_DB" "$output"
    else
      printf '{"data":[],"has_more":false,"after":null}\n' > "$output"
    fi
    ;;
  */api/agents\?limit=100\&cursor=*)
    test "$method" = GET
    test "$auth" = "Authorization: Bearer mock-jwt-token"
    printf 'api:GET:%s\n' "${url#http://127.0.0.1:3080}" >> "$DEPLOY_TEST_LOG"
    if [[ -n "${DEPLOY_TEST_AGENTS_PAGE2:-}" && -f "$DEPLOY_TEST_AGENTS_PAGE2" ]]; then
      cp "$DEPLOY_TEST_AGENTS_PAGE2" "$output"
    else
      printf '{"data":[],"has_more":false,"after":null}\n' > "$output"
    fi
    ;;
  */api/agents/*/expanded)
    test "$method" = GET
    test "$auth" = "Authorization: Bearer mock-jwt-token"
    agent_id=${url%/expanded}
    agent_id=${agent_id##*/}
    jq -s --arg id "$agent_id" '([.[].data[]? | select(.id == $id)] | .[0]) // empty' \
      "$DEPLOY_TEST_AGENTS_DB" "${DEPLOY_TEST_AGENTS_PAGE2:-$DEPLOY_TEST_AGENTS_DB}" > "$output"
    test -s "$output"
    ;;
  */api/agents|*/api/agents/*)
    test "$auth" = "Authorization: Bearer mock-jwt-token"
    name=$(jq -r .name <<< "$body")
    jq -e '(.instructions | length > 20) and (.tools == ["gemini_image_gen", "sys__all__sys_mcp_zyb_studio"]) and (.support_contact == {name: "庄严宝刹", email: ""})' <<< "$body" >/dev/null
    if [[ "$name" == "庄严设计助手" ]]; then
      agent_id="agent_studio_main_id"
      oid="670000000000000000000001"
    else
      agent_id="agent_studio_fast_id"
      oid="670000000000000000000002"
    fi
    printf 'api:%s:%s:%s\n' "$method" "${url#http://127.0.0.1:3080}" "$agent_id" >> "$DEPLOY_TEST_LOG"
    jq --arg id "$agent_id" --arg oid "$oid" '. + {id: $id, _id: $oid}' <<< "$body" > "$output"
    ;;
  */api/permissions/agent/*)
    test "$method" = PUT
    test "$auth" = "Authorization: Bearer mock-jwt-token"
    jq -e '.public == true and .publicAccessRoleId == "agent_viewer" and (.updated[0] == {type:"role",id:"USER",accessRoleId:"agent_viewer"})' <<< "$body" >/dev/null
    printf 'api:PUT:%s\n' "${url#http://127.0.0.1:3080}" >> "$DEPLOY_TEST_LOG"
    printf '{"ok":true}\n' > "$output"
    ;;
  *)
    echo "Unexpected curl URL: $url" >&2
    exit 1
    ;;
esac
EOF
cat > "$work/bin/chown" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
test "$1" = 10001:10001
test -f "$2"
printf 'config-owner:%s\n' "${2##*/}" >> "$DEPLOY_TEST_LOG"
EOF
chmod 755 "$work/bin/docker" "$work/bin/curl" "$work/bin/chown"

printf 'previous compose\n' > "$work/previous/compose.yml"
printf 'previous config\n' > "$work/previous/librechat.yaml"
printf 'ALLOW_REGISTRATION=true\n' > "$work/previous/.env"
printf 'LIBRECHAT_IMAGE=ghcr.io/getio0909/librechat-dev@sha256:%064d\nSTUDIO_IMAGE=ghcr.io/getio0909/librechat-studio:sha-prev@sha256:%064d\n' 0 0 > "$work/previous/image.env"
printf '{"shared_token":"%s","file_signing_key":"%s","public_base_url":"https://dev.xuqian.io","enable_3d":false,"gemini_api_key":"prev-gemini","ark_api_key":"prev-ark"}\n' \
  "$(printf 'a%.0s' {1..64})" "$(printf 'c%.0s' {1..64})" > "$work/previous/studio.json"

printf 'new compose\n' > "$work/bundle/compose.yml"
cp "$source_dir/../librechat.yaml" "$work/bundle/librechat.yaml"
cp "$work/previous/.env" "$work/bundle/.env"
printf 'LIBRECHAT_IMAGE=ghcr.io/getio0909/librechat-dev@sha256:%s\nSTUDIO_IMAGE=ghcr.io/getio0909/librechat-studio:sha-next@sha256:%s\n' \
  "$(printf '1%.0s' {1..64})" "$(printf '2%.0s' {1..64})" > "$work/bundle/image.env"
printf '{"shared_token":"%s","file_signing_key":"%s","public_base_url":"https://dev.xuqian.io","enable_3d":false,"gemini_api_key":"next-gemini","ark_api_key":"next-ark"}\n' \
  "$(printf 'b%.0s' {1..64})" "$(printf 'd%.0s' {1..64})" > "$work/bundle/studio.json"
printf 'test-token' > "$work/bundle/registry.token"
printf '%s\n' '{"admin":{"email":"admin@example.test","username":"admin","password":"test-password"},"user":{"email":"user@example.test","username":"user","password":"test-password"}}' > "$work/bundle/accounts.json"
tar -czf "$work/bundle.tgz" -C "$work/bundle" .

printf '%s\n' '{"data":[{"id":"agent_studio_main_id","_id":"670000000000000000000001","name":"庄严设计助手","instructions":"owned <!-- librechat-studio-owner:zyb-design:v1 -->","tools":["gemini_image_gen","sys__all__sys_mcp_zyb_studio"]},{"id":"agent_studio_fast_id","_id":"670000000000000000000002","name":"庄严设计助手 · 快速","instructions":"owned <!-- librechat-studio-owner:zyb-design-fast:v1 -->","tools":["gemini_image_gen","sys__all__sys_mcp_zyb_studio"]}],"has_more":false,"after":null}' > "$work/existing-agents.json"

# 首次部署与重部署成功后，librechat.yaml 中的占位符应被替换为解析后的持久化 agent_id。
mkdir -p "$work/expected-first" "$work/redeployed"
cp "$work/bundle/compose.yml" "$work/bundle/.env" "$work/bundle/image.env" "$work/bundle/studio.json" "$work/expected-first/"
sed -e 's/agent___STUDIO_AGENT_ID_MAIN__/agent_studio_main_id/g' \
    -e 's/agent___STUDIO_AGENT_ID_FAST__/agent_studio_fast_id/g' \
    "$work/bundle/librechat.yaml" > "$work/expected-first/librechat.yaml"

cp "$work/bundle/compose.yml" "$work/expected-first/librechat.yaml" "$work/bundle/.env" "$work/bundle/image.env" "$work/redeployed/"
jq --arg key "$(printf 'c%.0s' {1..64})" '.file_signing_key = $key' "$work/bundle/studio.json" > "$work/redeployed/studio.json"

for scenario in none health command login role final first-none first-login first-role; do
  failure=${scenario#first-}
  root="$work/$scenario"
  mkdir "$root"
  if [[ "$scenario" != first-* ]]; then
    cp "$work/previous/"* "$work/previous/.env" "$root/"
    chmod 600 "$root/"* "$root/.env"
    export DEPLOY_TEST_AGENTS_DB="$work/existing-agents.json"
  else
    unset DEPLOY_TEST_AGENTS_DB
  fi
  : > "$DEPLOY_TEST_LOG"
  export DEPLOY_TEST_FAILURE="$failure"
  status=0
  SSH_ORIGINAL_COMMAND="deploy $root" bash "$work/deploy.sh" "$root" \
    < "$work/bundle.tgz" > "$work/output.log" 2>&1 || status=$?
  if [[ "$scenario" == first-* && "$failure" != none ]]; then
    test "$status" != 0
    grep -q 'The failed initial application deployment has been stopped.' "$work/output.log"
    grep -q '^down:' "$DEPLOY_TEST_LOG"
    for file in compose.yml librechat.yaml .env image.env studio.json .file_signing_key .studio_agent_ids.json; do test ! -f "$root/$file"; done
    test -f "$DEPLOY_TEST_GATE"
  elif [[ "$failure" == none ]]; then
    test "$status" = 0
    if [[ "$scenario" == first-none ]]; then
      expected="$work/expected-first"
      expected_signing_key="$(printf 'd%.0s' {1..64})"
      grep -q '^api:POST:/api/agents:agent_studio_main_id$' "$DEPLOY_TEST_LOG"
      grep -q '^api:POST:/api/agents:agent_studio_fast_id$' "$DEPLOY_TEST_LOG"
    else
      expected="$work/redeployed"
      expected_signing_key="$(printf 'c%.0s' {1..64})"
      grep -q '^api:PATCH:/api/agents/agent_studio_main_id:agent_studio_main_id$' "$DEPLOY_TEST_LOG"
      grep -q '^api:PATCH:/api/agents/agent_studio_fast_id:agent_studio_fast_id$' "$DEPLOY_TEST_LOG"
    fi
    grep -q '^api:PUT:/api/roles/USER/agents$' "$DEPLOY_TEST_LOG"
    grep -q '^api:PUT:/api/permissions/agent/670000000000000000000001$' "$DEPLOY_TEST_LOG"
    grep -q '^api:PUT:/api/permissions/agent/670000000000000000000002$' "$DEPLOY_TEST_LOG"
    ! grep -qE 'agent___STUDIO_AGENT_ID_(MAIN|FAST)__' "$root/librechat.yaml"
    test -f "$root/.studio_agent_ids.json"
    test "$(stat -c '%a' "$root/.studio_agent_ids.json")" = 600
    test "$(jq -r '.agent___STUDIO_AGENT_ID_MAIN__' "$root/.studio_agent_ids.json")" = "agent_studio_main_id"
    test "$(jq -r '.agent___STUDIO_AGENT_ID_FAST__' "$root/.studio_agent_ids.json")" = "agent_studio_fast_id"
    grep -q '^up:false:.*11111111' "$DEPLOY_TEST_LOG"
    test -f "$root/.file_signing_key"
    test "$(stat -c '%a' "$root/.file_signing_key")" = 600
    test "$(tr -d '\r\n' < "$root/.file_signing_key")" = "$expected_signing_key"
    test "$(jq -r '.file_signing_key' "$root/studio.json")" = "$expected_signing_key"
  else
    test "$status" != 0
    expected="$work/previous"
    grep -q 'The previous application deployment has been restored.' "$work/output.log"
    grep -q '^up:false:.*00000000' "$DEPLOY_TEST_LOG"
  fi
  grep -q '^up:true:.*11111111' "$DEPLOY_TEST_LOG"
  grep -q '^config-owner:studio.json$' "$DEPLOY_TEST_LOG"
  if [[ "$scenario" != first-* || "$failure" == none ]]; then
    for file in compose.yml librechat.yaml .env image.env studio.json; do
      cmp "$expected/$file" "$root/$file"
      test "$(stat -c '%a' "$root/$file")" = 600
    done
    test ! -f "$DEPLOY_TEST_GATE"
  fi
  test -z "$(find "$root" -maxdepth 1 -name '.incoming.*' -print -quit)"
  printf 'Deployment recovery test passed: %s\n' "$scenario"
done

# 验证分页拉取与归属保护：第一页含非 Studio 同名智能体且带分页游标，第二页含真实 Studio 智能体时绝不覆盖同名外部智能体。
printf '%s\n' '{"data":[{"id":"agent_unrelated_user_id","_id":"670000000000000000000099","name":"庄严设计助手","tools":["web_search"]}],"has_more":true,"after":"cursor_p2"}' > "$work/agents-page1.json"
cp "$work/existing-agents.json" "$work/agents-page2.json"
mkdir -p "$work/paginated"
cp "$work/previous/"* "$work/previous/.env" "$work/paginated/"
chmod 600 "$work/paginated/"* "$work/paginated/.env"
: > "$DEPLOY_TEST_LOG"
export DEPLOY_TEST_FAILURE=none
export DEPLOY_TEST_AGENTS_DB="$work/agents-page1.json"
export DEPLOY_TEST_AGENTS_PAGE2="$work/agents-page2.json"
SSH_ORIGINAL_COMMAND="deploy $work/paginated" bash "$work/deploy.sh" "$work/paginated" \
  < "$work/bundle.tgz" > "$work/output.log" 2>&1
unset DEPLOY_TEST_AGENTS_PAGE2
grep -q '^api:GET:/api/agents?limit=100$' "$DEPLOY_TEST_LOG"
grep -q '^api:GET:/api/agents?limit=100&cursor=cursor_p2$' "$DEPLOY_TEST_LOG"
! grep -q 'agent_unrelated_user_id' "$DEPLOY_TEST_LOG"
grep -q '^api:PATCH:/api/agents/agent_studio_main_id:agent_studio_main_id$' "$DEPLOY_TEST_LOG"
grep -q '^api:PATCH:/api/agents/agent_studio_fast_id:agent_studio_fast_id$' "$DEPLOY_TEST_LOG"
echo 'Deployment pagination and Studio agent ownership test passed.'

# 同名、同工具的外部智能体没有归属标记时，只允许新建；持久化 ID 若指向它则必须失败关闭。
printf '%s\n' '{"data":[{"id":"agent_foreign","_id":"670000000000000000000099","name":"庄严设计助手","instructions":"foreign instructions","tools":["gemini_image_gen","sys__all__sys_mcp_zyb_studio"]}],"has_more":false,"after":null}' > "$work/foreign-agents.json"
mkdir -p "$work/foreign-create" "$work/foreign-persisted" "$work/incomplete-page"
for scenario in foreign-create foreign-persisted incomplete-page; do
  cp "$work/previous/"* "$work/previous/.env" "$work/$scenario/"
  chmod 600 "$work/$scenario/"* "$work/$scenario/.env"
done
export DEPLOY_TEST_AGENTS_DB="$work/foreign-agents.json"
: > "$DEPLOY_TEST_LOG"
SSH_ORIGINAL_COMMAND="deploy $work/foreign-create" bash "$work/deploy.sh" "$work/foreign-create" \
  < "$work/bundle.tgz" > "$work/output.log" 2>&1
grep -q '^api:POST:/api/agents:agent_studio_main_id$' "$DEPLOY_TEST_LOG"
! grep -q '^api:PATCH:/api/agents/agent_foreign:' "$DEPLOY_TEST_LOG"

printf '%s\n' '{"agent___STUDIO_AGENT_ID_MAIN__":"agent_foreign"}' > "$work/foreign-persisted/.studio_agent_ids.json"
: > "$DEPLOY_TEST_LOG"
if SSH_ORIGINAL_COMMAND="deploy $work/foreign-persisted" bash "$work/deploy.sh" "$work/foreign-persisted" \
  < "$work/bundle.tgz" > "$work/output.log" 2>&1; then
  echo 'Foreign persisted agent was adopted.' >&2
  exit 1
fi
grep -q 'Persisted Studio agent ID has no matching ownership marker.' "$work/output.log"
! grep -qE '^api:(POST|PATCH):/api/agents' "$DEPLOY_TEST_LOG"

printf '%s\n' '{"data":[],"has_more":true,"after":null}' > "$work/incomplete-agents.json"
export DEPLOY_TEST_AGENTS_DB="$work/incomplete-agents.json"
: > "$DEPLOY_TEST_LOG"
if SSH_ORIGINAL_COMMAND="deploy $work/incomplete-page" bash "$work/deploy.sh" "$work/incomplete-page" \
  < "$work/bundle.tgz" > "$work/output.log" 2>&1; then
  echo 'Incomplete agent pagination was accepted.' >&2
  exit 1
fi
grep -q 'Studio agent listing returned incomplete pagination.' "$work/output.log"
! grep -qE '^api:(POST|PATCH):/api/agents' "$DEPLOY_TEST_LOG"
echo 'Deployment foreign ownership and incomplete pagination tests passed.'

# 验证首次部署生成的签名密钥在后续重部署时不会被新归档中的随机密钥覆盖。
mkdir -p "$work/bundle2"
cp "$work/bundle/"* "$work/bundle/.env" "$work/bundle2/"
jq --arg token "$(printf 'e%.0s' {1..64})" --arg key "$(printf 'f%.0s' {1..64})" \
  '.shared_token = $token | .file_signing_key = $key' "$work/bundle/studio.json" > "$work/bundle2/studio.json"
tar -czf "$work/bundle2.tgz" -C "$work/bundle2" .
export DEPLOY_TEST_FAILURE=none
export DEPLOY_TEST_AGENTS_DB="$work/existing-agents.json"
SSH_ORIGINAL_COMMAND="deploy $work/first-none" bash "$work/deploy.sh" "$work/first-none" \
  < "$work/bundle2.tgz" > "$work/output.log" 2>&1
test "$(jq -r '.shared_token' "$work/first-none/studio.json")" = "$(printf 'e%.0s' {1..64})"
test "$(jq -r '.file_signing_key' "$work/first-none/studio.json")" = "$(printf 'd%.0s' {1..64})"
test "$(tr -d '\r\n' < "$work/first-none/.file_signing_key")" = "$(printf 'd%.0s' {1..64})"
echo 'Deployment signing key persistence test passed.'

printf 'unexpected\n' > "$work/bundle/unexpected.txt"
tar -czf "$work/invalid.tgz" -C "$work/bundle" .
if SSH_ORIGINAL_COMMAND="deploy $root" bash "$work/deploy.sh" "$root" \
  < "$work/invalid.tgz" > "$work/output.log" 2>&1; then
  echo 'An unexpected archive member was accepted.' >&2
  exit 1
fi
grep -q 'Unexpected deployment archive member.' "$work/output.log"
test -z "$(find "$root" -maxdepth 1 -name '.incoming.*' -print -quit)"
echo 'Deployment archive rejection test passed.'
