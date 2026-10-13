#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
project="librechat-runtime-$RANDOM-$RANDOM"
compose=(docker compose -p "$project" -f "$work/compose.yml" --env-file "$work/.env")
cleanup() {
  status=$?
  trap - EXIT
  if (( status != 0 )); then "${compose[@]}" logs --tail 25 || true; fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf -- "$work"
  exit "$status"
}
trap cleanup EXIT
cp "$source_dir/compose.yml" "$work/compose.yml"
sed -i -e 's/127.0.0.1:5001/127.0.0.1:15001/' -e 's/127.0.0.1:5002/127.0.0.1:15002/' "$work/compose.yml"
printf 'LIBRECHAT_IMAGE=unused\nFREECAD_IMAGE=%s\nDESIGN_IMAGE=%s\nMEILI_MASTER_KEY=fixture\nRAG_DB_PASSWORD=fixture\n' \
  "${FREECAD_IMAGE:?}" "${DESIGN_IMAGE:?}" > "$work/.env"
for account in admin user; do
  id=$(printf 'a%.0s' {1..24})
  if [[ "$account" == user ]]; then id=$(printf 'b%.0s' {1..24}); fi
  printf '{"userId":"%s","workspaceUrl":"https://example.test/cad-files/%s/"}\n' "$id" "$account" > "$work/design-$account.json"
  printf '{"serve-path":"/data","bind":"0.0.0.0","port":5000,"path-prefix":"/cad-files/%s","auth":["%s:fixture-%s-password@/:rw"],"allow-upload":true,"allow-archive":true,"allow-symlink":false}\n' \
    "$account" "$account" "$account" > "$work/files-$account.yaml"
  chmod 600 "$work/design-$account.json" "$work/files-$account.yaml"
  if [[ $(id -u) == 0 ]]; then
    chown 10001:10001 "$work/design-$account.json" "$work/files-$account.yaml"
  else
    sudo chown 10001:10001 "$work/design-$account.json" "$work/files-$account.yaml"
  fi
  docker volume create --label "com.docker.compose.project=$project" \
    --label "com.docker.compose.volume=cad-$account" "${project}_cad-$account" >/dev/null
  docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
    -v "${project}_cad-$account:/work" "$DESIGN_IMAGE" -c '
      mkdir -p /work/legacy
      printf "legacy model" > /work/legacy/model.step
      chmod 700 /work/legacy
      chmod 600 /work/legacy/model.step
      ln -s /usr/local/bin/node /work/no-follow.step
      ln -s /run/secrets/files.yaml /work/private-config.yaml
    ' < /dev/null
done
"${compose[@]}" up -d --wait --wait-timeout 180 \
  freecad-admin freecad-user blender-admin blender-user design-admin design-user files-admin files-user

for account in admin user; do
  port=15001
  if [[ "$account" == user ]]; then port=15002; fi
  curl --fail --silent --show-error --user "$account:fixture-$account-password" \
    -o /dev/null "http://127.0.0.1:$port/cad-files/$account/?json"
  "${compose[@]}" exec -T "freecad-$account" node -e '
    if (process.getuid() !== 10001) process.exit(1);
    const fs = require("node:fs"), assert = require("node:assert/strict");
    assert.equal(fs.readFileSync("/tmp/cad/legacy/model.step", "utf8"), "legacy model");
    fs.writeFileSync("/tmp/cad/legacy/model.step", "edited model");
    fs.writeFileSync("/tmp/cad/legacy/new.step", "new model");
    assert.equal(fs.statSync("/usr/local/bin/node").uid, 0);
    assert.equal(fs.lstatSync("/tmp/cad/no-follow.step").isSymbolicLink(), true);
    fs.writeFileSync("/tmp/cad/native-write.step", "fixture");
  ' < /dev/null
  "${compose[@]}" exec -T -e "ACCOUNT=$account" "design-$account" node --input-type=module <<'NODE'
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import dns from 'node:dns/promises';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { SSEClientTransport } from '@modelcontextprotocol/sdk/client/sse.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
const account = process.env.ACCOUNT;
const other = account === 'admin' ? 'user' : 'admin';
const settings = JSON.parse(await fs.readFile('/run/secrets/design.json', 'utf8'));
assert.equal(process.getuid(), 10001);
await assert.rejects(dns.lookup('blender-' + other));
const clients = [];
async function connect(name, transport) {
  const client = new Client({ name: 'runtime-test', version: '1' });
  clients.push(client);
  await client.connect(transport);
  assert.ok((await client.listTools()).tools.length > 0);
  console.log(`${account}: ${name} MCP connected.`);
  return client;
}
function data(result) {
  assert.notEqual(result.isError, true);
  return JSON.parse(result.content.find(item => item.type === 'text').text);
}
try {
  const cad = await connect('FreeCAD', new SSEClientTransport(new URL(`http://freecad-${account}:8000/sse`)));
  const native = data(await cad.callTool({ name: 'check_freecad_connection', arguments: {} }));
  assert.ok(native.instances.some(instance => instance.available));
  const blender = await connect('Blender', new StreamableHTTPClientTransport(new URL(`http://blender-${account}:10101/`)));
  await blender.callTool({ name: 'welcome', arguments: {} });
  const exported = data(await blender.callTool({ name: 'media_io', arguments: { verb: 'export', args: { format: 'stl', filename: 'cube.stl', objects: ['Cube'] } } }));
  assert.equal(exported.status, 'ok');
  assert.equal(exported.result.folder, '/tmp/cad');
  const imported = data(await blender.callTool({ name: 'media_io', arguments: { verb: 'import', args: { name: exported.result.file } } }));
  assert.equal(imported.status, 'ok');
  const fileUrl = `http://files-${account}:5000/cad-files/${account}/`;
  const headers = { Authorization: 'Basic ' + Buffer.from(`${account}:fixture-${account}-password`).toString('base64') };
  const upload = await fetch(fileUrl + 'drawing.svg', { method: 'PUT', headers, body: '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"><rect x="100" y="100" width="600" height="400" fill="none" stroke="black" stroke-width="4"/></svg>' });
  assert.ok(upload.ok);
  const design = await connect('Design', new StreamableHTTPClientTransport(new URL('http://127.0.0.1:8000/mcp'), { requestInit: { headers: { 'X-LibreChat-User-ID': settings.userId } } }));
  data(await design.callTool({ name: 'export_drawing_pdf', arguments: { source_filename: 'drawing.svg', pdf_filename: 'drawing.pdf' } }));
  const listing = data(await design.callTool({ name: 'workspace_files', arguments: {} }));
  assert.ok(listing.files.some(file => file.filename === 'cube.stl'));
  assert.ok(listing.archive_url.endsWith('/?zip'));
  const pdf = await fetch(fileUrl + 'drawing.pdf', { headers });
  assert.equal(pdf.status, 200);
  assert.equal(Buffer.from(await pdf.arrayBuffer()).subarray(0, 5).toString(), '%PDF-');
  const zip = await fetch(fileUrl + '?zip', { headers });
  assert.equal(zip.status, 200);
  assert.equal(Buffer.from(await zip.arrayBuffer()).subarray(0, 2).toString(), 'PK');
  const denied = await fetch(fileUrl + 'drawing.pdf');
  assert.equal(denied.status, 401);
  const escaped = await fetch(fileUrl + 'private-config.yaml', { headers });
  assert.ok([403, 404].includes(escaped.status));
  const wrong = await fetch(fileUrl + 'drawing.pdf', { headers: { Authorization: 'Basic ' + Buffer.from(`${other}:fixture-${other}-password`).toString('base64') } });
  assert.equal(wrong.status, 401);
  console.log(`${account}: private native import/export, PDF, ZIP, and file authentication passed.`);
} finally {
  await Promise.all(clients.map(client => client.close()));
}
NODE
done
