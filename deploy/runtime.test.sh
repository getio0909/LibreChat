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
sed -i -e 's/127.0.0.1:5001/127.0.0.1:15001/' -e 's/127.0.0.1:5002/127.0.0.1:15002/' \
  -e 's/127.0.0.1:5003/127.0.0.1:15003/' -e 's/127.0.0.1:5004/127.0.0.1:15004/' "$work/compose.yml"
printf 'LIBRECHAT_IMAGE=unused\nFREECAD_IMAGE=%s\nDESIGN_IMAGE=%s\nMEILI_MASTER_KEY=fixture\nRAG_DB_PASSWORD=fixture\n' \
  "${FREECAD_IMAGE:?}" "${DESIGN_IMAGE:?}" > "$work/.env"
for account in admin user; do
  id=$(printf 'a%.0s' {1..24})
  if [[ "$account" == user ]]; then id=$(printf 'b%.0s' {1..24}); fi
  printf '{"userId":"%s","workspaceUrl":"https://example.test/cad-files/%s/","downloadUrl":"http://127.0.0.1:8000/download/","downloadSigningKey":"%s"}\n' \
    "$id" "$account" "$(printf 'a%.0s' {1..64})" > "$work/design-$account.json"
  printf '{"serve-path":"/data","bind":"0.0.0.0","port":5000,"path-prefix":"/cad-files/%s","auth":["%s:fixture-%s-password@/:rw"],"allow-upload":true,"allow-archive":true,"allow-symlink":false,"hidden":[".????????-????-????-????-????????????.tmp"]}\n' \
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
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { PDFDocument, PDFName, PDFRawStream } from 'pdf-lib';
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
  const cad = await connect('FreeCAD', new StreamableHTTPClientTransport(new URL(`http://freecad-${account}:8000/mcp`)));
  const native = data(await cad.callTool({ name: 'check_freecad_connection', arguments: {} }));
  assert.ok(native.instances.some(instance => instance.available));
  const guidance = await cad.callTool({ name: 'get_usage_guidance', arguments: {} });
  assert.match(JSON.stringify(guidance), /cad_check\.audit_document/);
  const blender = await connect('Blender', new StreamableHTTPClientTransport(new URL(`http://blender-${account}:10101/`)));
  await blender.callTool({ name: 'welcome', arguments: {} });
  const exported = data(await blender.callTool({ name: 'media_io', arguments: { verb: 'export', args: { format: 'stl', filename: 'cube.stl', objects: ['Cube'] } } }));
  assert.equal(exported.status, 'ok');
  assert.equal(exported.result.folder, '/tmp/cad');
  const imported = data(await blender.callTool({ name: 'media_io', arguments: { verb: 'import', args: { name: exported.result.file } } }));
  assert.equal(imported.status, 'ok');
  const fileUrl = `http://files-${account}:5000/cad-files/${account}/`;
  const headers = { Authorization: 'Basic ' + Buffer.from(`${account}:fixture-${account}-password`).toString('base64') };
  const staged = '.12345678-1234-1234-1234-123456789abc.tmp';
  await fs.writeFile('/tmp/cad/' + staged, 'incomplete export fixture');
  await fs.writeFile('/tmp/cad/.notes.tmp', 'unrelated temporary file');
  const filesListing = await fetch(fileUrl + '?json', { headers });
  assert.equal(filesListing.status, 200);
  const filesText = await filesListing.text();
  assert.ok(!filesText.includes(staged));
  assert.ok(filesText.includes('.notes.tmp'));
  const upload = await fetch(fileUrl + 'drawing.svg', { method: 'PUT', headers, body: '<svg xmlns="http://www.w3.org/2000/svg" width="420mm" height="297mm" viewBox="0 0 420 297"><rect x="40" y="40" width="200" height="100" fill="none" stroke="black" stroke-width="0.3"/><text x="40" y="30" font-family="Noto Sans CJK SC" font-size="5">连接详图</text></svg>' });
  assert.ok(upload.ok);
  const design = await connect('Design', new StreamableHTTPClientTransport(new URL('http://127.0.0.1:8000/mcp'), { requestInit: { headers: { 'X-LibreChat-User-ID': settings.userId } } }));
  data(await design.callTool({ name: 'compile_drawing_sheet', arguments: { svg_filename: 'compiled-sheet.svg', title_block: { project_name: '禅意博古柜', sheet_title: '总体布置图', drawing_no: 'ZG-01' }, views: [{ id: 'front', title: '正立面图', scale: '1:15', origin_mm: [24, 42], rects: [{ x: 0, y: 0, w: 2000, h: 2200 }], chain_dims: [{ orientation: 'horizontal', start: 0, anchor: 0, offset_mm: -8, segments: [18, 1964, 18], expected_total: 2000 }] }] } }));
  data(await design.callTool({ name: 'export_drawing_pdf', arguments: { source_filename: 'drawing.svg', pdf_filename: 'drawing.pdf', page_width_mm: 420, page_height_mm: 297 } }));
  const set = data(await design.callTool({ name: 'assemble_drawing_pdf', arguments: { pdf_filename: 'set.pdf', sheets: [{ source_filename: 'drawing.pdf' }, { source_filename: 'compiled-sheet.svg', page_width_mm: 420, page_height_mm: 297 }] } }));
  assert.equal(set.page_count, 2);
  const document = await PDFDocument.load(await fs.readFile('/tmp/cad/set.pdf'));
  assert.equal(document.getPageCount(), 2);
  assert.ok(Math.abs(document.getPage(1).getWidth() - 420 * 72 / 25.4) < 0.001);
  assert.equal(document.context.enumerateIndirectObjects().filter(([, object]) => object instanceof PDFRawStream && object.dict.get(PDFName.of('Subtype'))?.toString() === '/Image').length, 0);
  for (const page of [1, 2]) {
    const inspection = await design.callTool({ name: 'inspect_pdf', arguments: { pdf_filename: 'set.pdf', page } });
    assert.equal(data(inspection).preview_source, 'finished_pdf');
    assert.ok(inspection.content.some(item => item.type === 'image'));
  }
  const listing = data(await design.callTool({ name: 'workspace_files', arguments: {} }));
  assert.ok(listing.files.some(file => file.filename === 'cube.stl'));
  assert.ok(listing.archive_url.endsWith('/?zip'));
  const signedUrl = listing.files.find(file => file.filename === 'drawing.pdf').download_url;
  const direct = await fetch(signedUrl);
  assert.equal(direct.status, 200);
  assert.equal(direct.headers.get('cache-control'), 'private, no-store');
  assert.equal(Buffer.from(await direct.arrayBuffer()).subarray(0, 5).toString(), '%PDF-');
  assert.equal((await fetch('http://127.0.0.1:8000/download/drawing.pdf')).status, 403);
  const pdf = await fetch(fileUrl + 'drawing.pdf', { headers });
  assert.equal(pdf.status, 200);
  assert.equal(Buffer.from(await pdf.arrayBuffer()).subarray(0, 5).toString(), '%PDF-');
  const zip = await fetch(fileUrl + '?zip', { headers });
  assert.equal(zip.status, 200);
  const archive = Buffer.from(await zip.arrayBuffer());
  assert.equal(archive.subarray(0, 2).toString(), 'PK');
  assert.ok(!archive.includes(Buffer.from(staged)));
  assert.ok(archive.includes(Buffer.from('.notes.tmp')));
  const denied = await fetch(fileUrl + 'drawing.pdf');
  assert.equal(denied.status, 401);
  const escaped = await fetch(fileUrl + 'private-config.yaml', { headers });
  assert.ok([403, 404].includes(escaped.status));
  const wrong = await fetch(fileUrl + 'drawing.pdf', { headers: { Authorization: 'Basic ' + Buffer.from(`${other}:fixture-${other}-password`).toString('base64') } });
  assert.equal(wrong.status, 401);
  console.log(`${account}: private native import/export, vector drawing sets, PDF inspection, ZIP, and file authentication passed.`);
} finally {
  await Promise.all(clients.map(client => client.close()));
}
NODE
done

for account in admin user; do
  port=15003
  if [[ "$account" == user ]]; then port=15004; fi
  status=$(curl --silent --show-error -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:$port/download/drawing.pdf")
  test "$status" = 403
done
