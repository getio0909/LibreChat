#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/bin" "$work/bundle" "$work/previous"
export PATH="$work/bin:$PATH"
export DEPLOY_TEST_LOG="$work/operations.log"

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
    up|exec|config|pull|ps) operation=$1; shift; break ;;
    *) shift ;;
  esac
done
image=$(cat "$root/image.env")
printf '%s:%s:%s\n' "$operation" "$closed" "$image" >> "$DEPLOY_TEST_LOG"
case "$operation" in
  up)
    if [[ "$image" == *11111111* ]]; then
      if [[ "$DEPLOY_TEST_FAILURE" == health || ( "$DEPLOY_TEST_FAILURE" == final && "$closed" == false ) ]]; then
        exit 1
      fi
    fi
    ;;
  exec)
    "$closed"
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
chmod 755 "$work/bin/docker" "$work/bin/curl"
printf 'previous compose\n' > "$work/previous/compose.yml"
printf 'previous config\n' > "$work/previous/librechat.yaml"
printf 'ALLOW_REGISTRATION=true\n' > "$work/previous/.env"
printf 'LIBRECHAT_IMAGE=ghcr.io/getio0909/librechat-dev@sha256:%064d\n' 0 > "$work/previous/image.env"
printf 'new compose\n' > "$work/bundle/compose.yml"
printf 'new config\n' > "$work/bundle/librechat.yaml"
cp "$work/previous/.env" "$work/bundle/.env"
printf 'LIBRECHAT_IMAGE=ghcr.io/getio0909/librechat-dev@sha256:%s\n' "$(printf '1%.0s' {1..64})" > "$work/bundle/image.env"
printf 'test-token' > "$work/bundle/registry.token"
printf '%s\n' '{"admin":{"email":"admin@example.test","username":"admin","password":"test-password"},"user":{"email":"user@example.test","username":"user","password":"test-password"}}' > "$work/bundle/accounts.json"
tar -czf "$work/bundle.tgz" -C "$work/bundle" .

for failure in none health command login role final; do
  root="$work/$failure"
  mkdir "$root"
  cp "$work/previous/"* "$work/previous/.env" "$root/"
  : > "$DEPLOY_TEST_LOG"
  export DEPLOY_TEST_FAILURE="$failure"
  status=0
  SSH_ORIGINAL_COMMAND="deploy $root" bash "$source_dir/deploy.sh" "$root" \
    < "$work/bundle.tgz" > "$work/output.log" 2>&1 || status=$?
  if [[ "$failure" == none ]]; then
    test "$status" = 0
    expected="$work/bundle"
    grep -q '^up:false:.*11111111' "$DEPLOY_TEST_LOG"
  else
    test "$status" != 0
    expected="$work/previous"
    grep -q 'The previous application deployment has been restored.' "$work/output.log"
    grep -q '^up:false:.*00000000' "$DEPLOY_TEST_LOG"
  fi
  grep -q '^up:true:.*11111111' "$DEPLOY_TEST_LOG"
  for file in compose.yml librechat.yaml .env image.env; do
    cmp "$expected/$file" "$root/$file"
    test "$(stat -c '%a' "$root/$file")" = 600
  done
  test -z "$(find "$root" -maxdepth 1 -name '.incoming.*' -print -quit)"
  printf 'Deployment recovery test passed: %s\n' "$failure"
done

printf 'unexpected\n' > "$work/bundle/unexpected.txt"
tar -czf "$work/invalid.tgz" -C "$work/bundle" .
if SSH_ORIGINAL_COMMAND="deploy $root" bash "$source_dir/deploy.sh" "$root" \
  < "$work/invalid.tgz" > "$work/output.log" 2>&1; then
  echo 'An unexpected archive member was accepted.' >&2
  exit 1
fi
grep -q 'Unexpected deployment archive member.' "$work/output.log"
test -z "$(find "$root" -maxdepth 1 -name '.incoming.*' -print -quit)"
echo 'Deployment archive rejection test passed.'
