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
image=$(cat "$root/image.env")
printf '%s:%s:%s\n' "$operation" "$closed" "$image" >> "$DEPLOY_TEST_LOG"
case "$operation" in
  up)
    if [[ "$image" == *00000000* ]]; then
      [[ " $* " == *' --force-recreate '* ]]
    fi
    if [[ "$*" == *'files-admin files-user design-admin design-user api'* ]]; then
      [[ " $* " == *' --force-recreate '* ]]
      printf 'reload-configs\n' >> "$DEPLOY_TEST_LOG"
    fi
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
    if [[ "$*" == *'config.mcpSettings.allowedAddresses'* ]]; then
      cat "$root/librechat.yaml"
      exit 0
    fi
    if [[ "$*" != *'/app/config/create-user.js'* ]]; then
      if [[ "$DEPLOY_TEST_FAILURE" == mcp ]]; then exit 1; fi
      exit 0
    fi
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
id=$(printf 'b%.0s' {1..24})
if [[ "$email" == admin@example.test ]]; then id=$(printf 'a%.0s' {1..24}); fi
jq -n --arg role "$role" --arg id "$id" '{user: {role: $role, id: $id}}' > "$output"
EOF
chmod 755 "$work/bin/docker" "$work/bin/curl"
printf 'previous compose\n' > "$work/previous/compose.yml"
printf 'previous config\n' > "$work/previous/librechat.yaml"
printf 'ALLOW_REGISTRATION=true\n' > "$work/previous/.env"
printf 'LIBRECHAT_IMAGE=ghcr.io/getio0909/librechat-dev@sha256:%064d\n' 0 > "$work/previous/image.env"
cp -r "$work/previous" "$work/previous-cad"
for account in admin user; do
  printf '{"fixture":"previous provider settings"}\n' > "$work/previous-cad/design-$account.json"
  printf '{"fixture":"previous file credentials"}\n' > "$work/previous-cad/files-$account.yaml"
done
printf 'new compose\n' > "$work/bundle/compose.yml"
printf 'new config\n' > "$work/bundle/librechat.yaml"
cp "$work/previous/.env" "$work/bundle/.env"
cp "$work/bundle/.env" "$work/expected.env"
printf 'CAD_ADMIN_USER_ID=%s\nCAD_USER_USER_ID=%s\n' "$(printf 'a%.0s' {1..24})" "$(printf 'b%.0s' {1..24})" >> "$work/expected.env"
printf 'LIBRECHAT_IMAGE=ghcr.io/getio0909/librechat-dev@sha256:%s\n' "$(printf '1%.0s' {1..64})" > "$work/bundle/image.env"
printf 'test-token' > "$work/bundle/registry.token"
printf '%s\n' '{"admin":{"email":"admin@example.test","username":"admin","password":"test-password"},"user":{"email":"user@example.test","username":"user","password":"test-password"}}' > "$work/bundle/accounts.json"
for account in admin user; do
  printf '{"workspaceUrl":"https://example.test/cad-files/%s/"}\n' "$account" > "$work/bundle/design-$account.json"
  printf '{"fixture":true}\n' > "$work/bundle/files-$account.yaml"
done
tar -czf "$work/bundle.tgz" -C "$work/bundle" .

for scenario in none health mcp command login role final first-none first-login first-role rotated-mcp rotated-final; do
  failure=${scenario#first-}
  failure=${failure#rotated-}
  previous="$work/previous"
  if [[ "$scenario" == rotated-* ]]; then previous="$work/previous-cad"; fi
  root="$work/$scenario"
  mkdir "$root"
  if [[ "$scenario" != first-* ]]; then
    cp "$previous/"* "$previous/.env" "$root/"
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
    for file in compose.yml librechat.yaml .env image.env design-admin.json design-user.json files-admin.yaml files-user.yaml; do test ! -f "$root/$file"; done
    test -f "$DEPLOY_TEST_GATE"
  elif [[ "$failure" == none ]]; then
    test "$status" = 0
    expected="$work/bundle"
    grep -q '^up:false:.*11111111' "$DEPLOY_TEST_LOG"
    grep -q '^reload-configs$' "$DEPLOY_TEST_LOG"
  else
    test "$status" != 0
    expected="$previous"
    grep -q 'The previous application deployment has been restored.' "$work/output.log"
    grep -q '^up:false:.*00000000' "$DEPLOY_TEST_LOG"
  fi
  grep -q '^up:true:.*11111111' "$DEPLOY_TEST_LOG"
  if [[ "$scenario" != first-* || "$failure" == none ]]; then
    for file in compose.yml librechat.yaml .env image.env; do
      if [[ "$file" == .env && "$failure" == none ]]; then
        cmp "$work/expected.env" "$root/$file"
      else
        cmp "$expected/$file" "$root/$file"
      fi
      test "$(stat -c '%a' "$root/$file")" = 600
    done
    test ! -f "$DEPLOY_TEST_GATE"
    for account in admin user; do
      if [[ "$failure" == none ]]; then
        jq -e '.userId | test("^[a-f0-9]{24}$")' "$root/design-$account.json" >/dev/null
        cmp "$work/bundle/files-$account.yaml" "$root/files-$account.yaml"
        test "$(stat -c '%a' "$root/design-$account.json")" = 600
        test "$(stat -c '%a' "$root/files-$account.yaml")" = 600
      elif [[ "$scenario" == rotated-* ]]; then
        cmp "$previous/design-$account.json" "$root/design-$account.json"
        cmp "$previous/files-$account.yaml" "$root/files-$account.yaml"
      else
        test ! -f "$root/design-$account.json"
        test ! -f "$root/files-$account.yaml"
      fi
    done
  fi
  test -z "$(find "$root" -maxdepth 1 -name '.incoming.*' -print -quit)"
  printf 'Deployment recovery test passed: %s\n' "$scenario"
done

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
