#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/bin" "$work/tls" "$work/input"

# 执行实际证书更新代码，仅替代 Nginx 的系统依赖；证书和文件操作保持真实。
printf '#!/usr/bin/env bash\nset -euo pipefail\n' > "$work/update.sh"
sed -n '/^tls_root=/,/^trap - EXIT/p' "$source_dir/bootstrap.sh" | \
  sed "s|^tls_root=.*|tls_root='$work/tls'|" >> "$work/update.sh"
test -s "$work/update.sh"
cat > "$work/bin/nginx" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
test "${TLS_TEST_NGINX_FAIL:-false}" = false
EOF
chmod 755 "$work/bin/nginx"
export PATH="$work/bin:$PATH"
for pair in first second; do
  openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=dev.xuqian.io \
    -keyout "$work/$pair.key" -out "$work/$pair.pem" >/dev/null 2>&1
done
update() {
  cp "$work/$1.pem" "$work/input/origin.pem"
  cp "$work/$2.key" "$work/input/origin.key"
  source_dir="$work/input" bash "$work/update.sh" > "$work/output.log" 2>&1
}
update first first
previous=$(readlink "$work/tls/current")
cmp "$work/first.pem" "$work/tls/current/dev.xuqian.io.pem"
test "$(stat -c '%a' "$work/tls/current/dev.xuqian.io.key")" = 600

if update second first; then
  echo 'Mismatched TLS credentials were accepted.' >&2
  exit 1
fi
test "$(readlink "$work/tls/current")" = "$previous"
cmp "$work/first.key" "$work/tls/current/dev.xuqian.io.key"
printf 'TLS recovery test passed: mismatched key\n'

printf 'malformed key\n' > "$work/invalid.key"
if update second invalid; then
  echo 'Malformed TLS credentials were accepted.' >&2
  exit 1
fi
test "$(readlink "$work/tls/current")" = "$previous"
printf 'TLS recovery test passed: malformed key\n'

export TLS_TEST_NGINX_FAIL=true
if update second second; then
  echo 'Rejected Nginx TLS configuration was activated.' >&2
  exit 1
fi
test "$(readlink "$work/tls/current")" = "$previous"
export TLS_TEST_NGINX_FAIL=false
printf 'TLS recovery test passed: rejected configuration\n'

update second second
test "$(readlink "$work/tls/current")" != "$previous"
cmp "$work/second.pem" "$work/tls/current/dev.xuqian.io.pem"
cmp "$work/second.key" "$work/tls/current/dev.xuqian.io.key"
test -f "$work/tls/$previous/dev.xuqian.io.key"
test -z "$(find "$work/tls" -maxdepth 1 -name '.tls.*' -print -quit)"
printf 'TLS recovery test passed: validated pair rotation\n'
