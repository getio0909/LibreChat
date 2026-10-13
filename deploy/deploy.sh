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
trap 'rm -rf -- "$stage"' EXIT
cat > "$stage/bundle.tgz"

# 在解包之前限制归档成员，防止部署凭据被用于写入其他路径。
while IFS= read -r member; do
  case "$member" in
    ./|./compose.yml|./librechat.yaml|./.env|./image.env|./registry.token) ;;
    *) echo 'Unexpected deployment archive member.' >&2; exit 1 ;;
  esac
done < <(tar -tzf "$stage/bundle.tgz")
tar -xzf "$stage/bundle.tgz" -C "$stage" --no-same-owner --no-same-permissions
for file in compose.yml librechat.yaml .env image.env registry.token; do
  if [[ ! -f "$stage/$file" || -L "$stage/$file" ]]; then
    echo 'A required deployment file is missing or is not a regular file.' >&2
    exit 1
  fi
done
if ! grep -Eq '^LIBRECHAT_IMAGE=ghcr\.io/getio0909/librechat-dev@sha256:[0-9a-f]{64}$' "$stage/image.env"; then
  echo 'The deployment must reference an immutable development image digest.' >&2
  exit 1
fi

# 拉取镜像所需的短期令牌只保存在本次部署的临时目录中。
export DOCKER_CONFIG="$stage/docker"
docker login ghcr.io -u getio0909 --password-stdin < "$stage/registry.token"
docker compose -p librechat-dev --project-directory "$stage" --env-file "$stage/.env" \
  --env-file "$stage/image.env" -f "$stage/compose.yml" config --quiet
docker compose -p librechat-dev --project-directory "$stage" --env-file "$stage/.env" \
  --env-file "$stage/image.env" -f "$stage/compose.yml" pull

had_previous=false
if [[ -f "$deploy_root/image.env" ]]; then
  mkdir -p "$deploy_root/.previous"
  for file in compose.yml librechat.yaml .env image.env; do
    cp "$deploy_root/$file" "$deploy_root/.previous/$file"
  done
  had_previous=true
fi
for file in compose.yml librechat.yaml .env image.env; do
  install -m 600 "$stage/$file" "$deploy_root/$file"
done

compose=(docker compose -p librechat-dev --project-directory "$deploy_root" \
  --env-file "$deploy_root/.env" --env-file "$deploy_root/image.env" -f "$deploy_root/compose.yml")
if ! "${compose[@]}" up -d --wait --wait-timeout 240; then
  echo 'Deployment health checks failed.' >&2
  if "$had_previous"; then
    for file in compose.yml librechat.yaml .env image.env; do
      cp "$deploy_root/.previous/$file" "$deploy_root/$file"
    done
    "${compose[@]}" up -d --wait --wait-timeout 240
    echo 'The previous application deployment has been restored.' >&2
  fi
  exit 1
fi
"${compose[@]}" ps
echo 'Development deployment passed health checks.'
