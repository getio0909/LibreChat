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
while [[ "$1" != -o ]]; do shift; done
output=$2
email=$(jq -r .email)
if [[ "$DEPLOY_TEST_FAILURE" == login ]]; then exit 22; fi
role=USER
if [[ "$email" == admin@example.test && "$DEPLOY_TEST_FAILURE" != role ]]; then role=ADMIN; fi
jq -n --arg role "$role" '{user: {role: $role}}' > "$output"
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
printf '{"shared_token":"%s","file_signing_key":"%s","public_base_url":"https://dev.xuqian.io","gemini_api_key":"prev-gemini","ark_api_key":"prev-ark"}\n' \
  "$(printf 'a%.0s' {1..64})" "$(printf 'c%.0s' {1..64})" > "$work/previous/studio.json"

printf 'new compose\n' > "$work/bundle/compose.yml"
printf 'new config\n' > "$work/bundle/librechat.yaml"
cp "$work/previous/.env" "$work/bundle/.env"
printf 'LIBRECHAT_IMAGE=ghcr.io/getio0909/librechat-dev@sha256:%s\nSTUDIO_IMAGE=ghcr.io/getio0909/librechat-studio:sha-next@sha256:%s\n' \
  "$(printf '1%.0s' {1..64})" "$(printf '2%.0s' {1..64})" > "$work/bundle/image.env"
printf '{"shared_token":"%s","file_signing_key":"%s","public_base_url":"https://dev.xuqian.io","gemini_api_key":"next-gemini","ark_api_key":"next-ark"}\n' \
  "$(printf 'b%.0s' {1..64})" "$(printf 'd%.0s' {1..64})" > "$work/bundle/studio.json"
printf 'test-token' > "$work/bundle/registry.token"
printf '%s\n' '{"admin":{"email":"admin@example.test","username":"admin","password":"test-password"},"user":{"email":"user@example.test","username":"user","password":"test-password"}}' > "$work/bundle/accounts.json"
tar -czf "$work/bundle.tgz" -C "$work/bundle" .

# 重部署成功时应更新 studio.json 中的新配置，但保留服务器已持久化的 file_signing_key。
mkdir -p "$work/redeployed"
cp "$work/bundle/compose.yml" "$work/bundle/librechat.yaml" "$work/bundle/.env" "$work/bundle/image.env" "$work/redeployed/"
jq --arg key "$(printf 'c%.0s' {1..64})" '.file_signing_key = $key' "$work/bundle/studio.json" > "$work/redeployed/studio.json"

for scenario in none health command login role final first-none first-login first-role; do
  failure=${scenario#first-}
  root="$work/$scenario"
  mkdir "$root"
  if [[ "$scenario" != first-* ]]; then
    cp "$work/previous/"* "$work/previous/.env" "$root/"
    chmod 600 "$root/"* "$root/.env"
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
    for file in compose.yml librechat.yaml .env image.env studio.json .file_signing_key; do test ! -f "$root/$file"; done
    test -f "$DEPLOY_TEST_GATE"
  elif [[ "$failure" == none ]]; then
    test "$status" = 0
    if [[ "$scenario" == first-none ]]; then
      expected="$work/bundle"
      expected_signing_key="$(printf 'd%.0s' {1..64})"
    else
      expected="$work/redeployed"
      expected_signing_key="$(printf 'c%.0s' {1..64})"
    fi
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

# 验证首次部署生成的签名密钥在后续重部署时不会被新归档中的随机密钥覆盖。
mkdir -p "$work/bundle2"
cp "$work/bundle/"* "$work/bundle/.env" "$work/bundle2/"
jq --arg token "$(printf 'e%.0s' {1..64})" --arg key "$(printf 'f%.0s' {1..64})" \
  '.shared_token = $token | .file_signing_key = $key' "$work/bundle/studio.json" > "$work/bundle2/studio.json"
tar -czf "$work/bundle2.tgz" -C "$work/bundle2" .
export DEPLOY_TEST_FAILURE=none
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
