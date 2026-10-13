#!/usr/bin/env bash
set -euo pipefail

deploy_root="${1:?Supply the deployment directory}"
source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
if [[ $(id -u) != 0 || ! "$deploy_root" =~ ^/opt/[a-zA-Z0-9_/-]+$ ]]; then
  echo 'Bootstrap requires root and a deployment directory under /opt.' >&2
  exit 1
fi

# 仅首次初始化时安装依赖，后续部署复用服务器已有的运行时。
if ! command -v nginx >/dev/null || ! command -v jq >/dev/null || \
  ! command -v curl >/dev/null || ! command -v openssl >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl nginx jq openssl
fi
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  if [[ $(. /etc/os-release && printf '%s' "$ID") != debian ]]; then
    echo 'Automatic Docker installation currently supports Debian servers.' >&2
    exit 1
  fi
  install -d -m 755 /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod 644 /etc/apt/keyrings/docker.asc
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && printf '%s' "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

install -d -m 700 /etc/ssl/librechat-dev "$deploy_root"
install -d -m 755 /usr/local/lib/librechat
install -m 755 "$source_dir/deploy.sh" /usr/local/lib/librechat/deploy.sh
# 先验证证书和私钥，再通过单个链接切换整对凭据，保留旧版本供恢复。
tls_root=/etc/ssl/librechat-dev
tls_stage=$(mktemp -d "$tls_root/.tls.XXXXXX")
trap 'rm -rf -- "$tls_stage"' EXIT
openssl x509 -in "$source_dir/origin.pem" -checkhost dev.xuqian.io -noout
openssl x509 -in "$source_dir/origin.pem" -pubkey -noout > "$tls_stage/cert.pub"
openssl pkey -in "$source_dir/origin.key" -passin pass: -pubout > "$tls_stage/key.pub"
if ! cmp -s "$tls_stage/cert.pub" "$tls_stage/key.pub"; then
  echo 'The origin certificate and private key do not match.' >&2
  exit 1
fi
install -m 644 "$source_dir/origin.pem" "$tls_stage/dev.xuqian.io.pem"
install -m 600 "$source_dir/origin.key" "$tls_stage/dev.xuqian.io.key"
printf 'events {}\nhttp { server { listen 443 ssl; ssl_certificate %s/dev.xuqian.io.pem; ssl_certificate_key %s/dev.xuqian.io.key; } }\n' \
  "$tls_stage" "$tls_stage" > "$tls_stage/nginx.conf"
nginx -t -c "$tls_stage/nginx.conf"
tls_version=$(sha256sum "$tls_stage/dev.xuqian.io.pem" | cut -d ' ' -f 1)
install -d -m 700 "$tls_root/$tls_version"
install -m 644 "$tls_stage/dev.xuqian.io.pem" "$tls_root/$tls_version/dev.xuqian.io.pem"
install -m 600 "$tls_stage/dev.xuqian.io.key" "$tls_root/$tls_version/dev.xuqian.io.key"
ln -s "$tls_version" "$tls_stage/current"
mv -Tf "$tls_stage/current" "$tls_root/current"
rm -rf -- "$tls_stage"
trap - EXIT

# 每次都重新生成可信代理配置，修复旧版本可能留下的不完整文件。
realip_stage=$(mktemp -d "$source_dir/.realip.XXXXXX")
trap 'rm -rf -- "$realip_stage"' EXIT
for family in v4 v6; do
  curl -fsSL "https://www.cloudflare.com/ips-$family" -o "$realip_stage/ips-$family"
  if [[ ! -s "$realip_stage/ips-$family" ]]; then
    echo 'The Cloudflare proxy address list is empty.' >&2
    exit 1
  fi
  awk '{ print "set_real_ip_from " $0 ";" }' "$realip_stage/ips-$family" >> "$realip_stage/realip.conf"
done
printf 'real_ip_header CF-Connecting-IP;\nreal_ip_recursive on;\n' >> "$realip_stage/realip.conf"
printf 'events {}\nhttp { include %s; }\n' "$realip_stage/realip.conf" > "$realip_stage/nginx.conf"
nginx -t -c "$realip_stage/nginx.conf"
install -m 644 "$realip_stage/realip.conf" /etc/nginx/cloudflare-realip.conf.new
mv -f /etc/nginx/cloudflare-realip.conf.new /etc/nginx/cloudflare-realip.conf
rm -rf -- "$realip_stage"
trap - EXIT
cat > /etc/nginx/conf.d/librechat-upgrade.conf <<'EOF'
map $http_upgrade $librechat_connection {
    default upgrade;
    '' close;
}
EOF
install -m 644 "$source_dir/nginx.conf" /etc/nginx/sites-available/librechat-dev
ln -sfn /etc/nginx/sites-available/librechat-dev /etc/nginx/sites-enabled/librechat-dev
nginx -t
systemctl enable --now docker nginx
systemctl reload nginx

if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
  for family in v4 v6; do
    curl -fsSL "https://www.cloudflare.com/ips-$family" | while IFS= read -r network; do
      ufw allow proto tcp from "$network" to any port 80 >/dev/null
      ufw allow proto tcp from "$network" to any port 443 >/dev/null
    done
  done
fi
echo 'Development server bootstrap completed.'
