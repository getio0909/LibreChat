import { toolDraft } from './studio-tool-drafts.js?v=19.7';

const sides = [['north', '上边'], ['east', '右边'], ['south', '下边'], ['west', '左边']];
const sourceKinds = ['', '实测', '图纸', '产品资料', '设计目标'];
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (['value', 'checked', 'required', 'disabled', 'hidden'].includes(key)) node[key] = value;
    else if (key === 'class') node.className = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.flat().filter(v => v != null)); return node;
}
export function floorplanDefaults() {
  return { width_mm: 7300, length_mm: 9780, height_mm: 3000,
    width_mm_source: '室内平面尺寸图.jpg：7300标注', length_mm_source: '室内平面尺寸图.jpg：9780标注',
    width_mm_kind: '图纸', length_mm_kind: '图纸',
    height_mm_source: '试做假设，待实测确认', height_mm_kind: '', height_kind: 'assumed', accept_assumption: false,
    open_sides: ['north', 'east'], view: 'A', layout: 'booth' };
}
export function switchHeightKind(spec, height_kind) {
  if (spec.height_kind === height_kind) return spec;
  return { ...spec, height_kind, height_mm_source: '', height_mm_kind: '', accept_assumption: false };
}
export function floorplanViewIssue(spec) {
  return spec?.view === 'B' && (!Array.isArray(spec.open_sides) || !spec.open_sides.includes('north'))
    ? 'B 平视机位在平面图上边外侧，当前完整墙面会挡住室内。请按真实开口核对上边，或改选 C、D 室内视角；不要为出图虚设开口。' : '';
}
export function floorplanReceiptValid(value, sourceSha256) {
  const id = v => typeof v === 'string' && /^[a-f0-9]{32}$/.test(v);
  const sha = v => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v);
  return Boolean(value && id(value.task_id) && id(value.source_id) && sha(value.source_sha256)
    && value.source_sha256 === sourceSha256 && sha(value.stored_source_sha256) && sha(value.fingerprint)
    && Number.isSafeInteger(value.revision) && value.revision > 0 && Array.isArray(value.guides) && value.guides.length === 3
    && new Set(value.guides.map(row => row?.id)).size === 3
    && new Set(value.guides.map(row => row?.kind)).size === 3
    && value.guides.every(row => row && id(row.id) && row.id !== value.source_id && sha(row.sha256)
      && ['plan', 'structure', 'depth'].includes(row.kind)));
}
export function floorplanSpecMatch(actual, expected) {
  if (!actual || !expected) return false;
  for (const key of ['width_mm', 'length_mm', 'height_mm']) {
    if (!Number.isFinite(expected[key]) || actual[key] !== Math.round(expected[key] * 1000) / 1000
      || actual[key + '_source'] !== String(expected[key + '_source'] || '').trim()) return false;
  }
  for (const key of ['width_mm_kind', 'length_mm_kind', 'height_kind', 'view', 'layout'])
    if (actual[key] !== expected[key]) return false;
  if (expected.height_kind === 'known' && actual.height_mm_kind !== expected.height_mm_kind) return false;
  return Array.isArray(actual.open_sides) && Array.isArray(expected.open_sides)
    && JSON.stringify([...actual.open_sides].sort()) === JSON.stringify([...expected.open_sides].sort());
}
export function floorplanReceipt(task, sourceSha256) {
  const record = task?.floorplan;
  const value = record && { task_id: task.id, revision: task.revision, source_id: record.source_id,
    source_sha256: sourceSha256, stored_source_sha256: record.source_sha256, fingerprint: record.fingerprint,
    guides: record.guides?.map(row => ({ id: row.id, kind: row.guide_kind, sha256: row.source_sha256 })) };
  return floorplanMatchesReceipt(task, value) ? value : null;
}
export function floorplanMatchesReceipt(task, receipt) {
  if (!floorplanReceiptValid(receipt, receipt?.source_sha256) || task?.id !== receipt.task_id) return false;
  const record = task.floorplan, assets = task.edit_assets || [];
  return Boolean(record && record.fingerprint === receipt.fingerprint && record.source_id === receipt.source_id
    && record.source_sha256 === receipt.stored_source_sha256
    && assets.some(row => row.id === receipt.source_id && row.upload_mode === 'reference' && row.source_sha256 === receipt.source_sha256)
    && record.guides?.length === 3 && receipt.guides.every(expected => record.guides.some(row => row.id === expected.id
      && row.guide_kind === expected.kind && row.source_sha256 === expected.sha256)
      && assets.some(row => row.id === expected.id && row.source_sha256 === expected.sha256)));
}
export function floorplanDraftValid(value) {
  const spec = value?.spec, source = value?.source;
  if (!spec || !source || !['example', 'upload'].includes(source.kind) || !/^[a-f0-9]{64}$/.test(source.sha256)
    || typeof value.style !== 'string' || value.style.length > 80 || spec.accept_assumption !== false) return false;
  if (source.kind === 'upload' && (typeof source.name !== 'string' || source.name.length > 500
    || !['image/jpeg', 'image/png', 'image/webp'].includes(source.type) || !Number.isFinite(source.size) || source.size < 0 || source.size > 20 * 1024 * 1024)) return false;
  for (const key of ['width_mm', 'length_mm', 'height_mm']) {
    if (spec[key] !== '' && spec[key] !== null && !Number.isFinite(spec[key])
      || typeof spec[key + '_source'] !== 'string' || spec[key + '_source'].length > 200 || !sourceKinds.includes(spec[key + '_kind'])) return false;
  }
  return (value.receipt === undefined || floorplanReceiptValid(value.receipt, source.sha256))
    && ['known', 'assumed'].includes(spec.height_kind) && ['A', 'B', 'C', 'D'].includes(spec.view)
    && ['empty', 'booth'].includes(spec.layout) && Array.isArray(spec.open_sides) && spec.open_sides.length <= 4
    && new Set(spec.open_sides).size === spec.open_sides.length && spec.open_sides.every(side => sides.some(([key]) => key === side));
}
export function openFloorplan({ packs, prepare, continueSaved, completed }) {
  const existing = document.querySelector('.zf-dialog[open]');
  if (existing) { existing.focus(); return existing; }
  let spec = floorplanDefaults(), file = null, previewURL = null, working = false, dirty = false;
  let fileProof = null, expectedSource = null, exampleSha = '', exampleChecking = true, hashing = false, storageError = '', submitted = false;
  let receipt = null;
  const drafts = toolDraft('floorplan', ['new-rectangular-space'], floorplanDraftValid);
  const recovered = drafts.read();
  let recoveryPending = Boolean(recovered.value) || !recovered.ok;
  const dialog = el('dialog', { class: 'zs-tool-dialog zf-dialog', 'aria-label': '平面图试做' });
  const notice = el('p', { class: 'zd-notice', role: 'status', 'aria-live': 'polite' });
  const inform = (text, error = false) => {
    notice.textContent = text + (storageError ? ' ' + storageError : ''); notice.dataset.error = String(error || Boolean(storageError));
  };
  const digest = async data => {
    if (!globalThis.crypto?.subtle) throw Error('当前浏览器无法校验源图，不能暂存可恢复草稿。请使用 localhost 或 HTTPS。');
    return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', data)), n => n.toString(16).padStart(2, '0')).join('');
  };
  function persistDraft() {
    if (recoveryPending || !dirty && !receipt) return;
    const hadError = Boolean(storageError);
    const source = fileProof || expectedSource || { kind: 'example', sha256: exampleSha };
    if (!source.sha256) {
      storageError = '源图校验尚未完成，草稿暂不能恢复，请勿刷新。'; inform('填写尚未提交。', true); return;
    }
    const result = drafts.write({ source, spec: { ...spec, accept_assumption: false }, style: style.value,
      ...(receipt ? { receipt } : {}) });
    storageError = result.ok ? '' : result.error + ' 请勿刷新或关闭标签页。';
    if (!result.ok) inform(receipt ? '底稿已保存，但本标签页未能暂存续接记录。请打开已保存试例。' : '填写尚未提交。', true);
    else if (hadError) inform(receipt ? '底稿已保存，本标签页续接记录已暂存。' : '本标签页草稿已暂存，填写仍未提交。');
    return result;
  }
  function recoveryLock() {
    dialog.querySelectorAll('input,select,button').forEach(input => {
      input.disabled = working || hashing || (recoveryPending || submitted) && !input.closest('.zt-draft-recovery')
        && !input.closest('.zf-saved') && !input.closest('.zt-head');
      if (exampleChecking && !file && !expectedSource && (input.closest('.zf-controls') || input.type === 'submit')) input.disabled = true;
    });
    const height = controls.querySelector('[name=height_mm_kind]');
    if (height && spec.height_kind !== 'known') height.disabled = true;
    refreshViewState();
  }
  const image = el('img', { src: '/zyb/studio/floorplan-example', alt: '客户原始展位尺寸图', class: 'zf-source-image' });
  const original = el('a', { href: image.src, class: 'zs-btn quiet', target: '_blank', rel: 'noopener' }, '放大查看原图');
  const sourceInfo = el('p', { class: 'zs-help' }, '真实试例：2026年春季佛事展。原图和原方案页均保留在资料目录。');
  const form = el('form', { class: 'zf-form', novalidate: '' });
  const structureConfirmation = el('input', { type: 'checkbox', required: true, name: 'structure_confirmed',
    onchange: () => { dirty = true; persistDraft(); } });
  function changed() { dirty = true; structureConfirmation.checked = false; persistDraft(); refreshViewState(); }
  const confirmation = el('input', { type: 'checkbox', name: 'accept_assumption', required: true,
    onchange: event => { spec.accept_assumption = event.target.checked; dirty = true; persistDraft(); } });
  const picker = el('input', { type: 'file', accept: '.jpg,.jpeg,.png,.webp', 'aria-label': '选择自己的平面图',
    onchange: async event => {
      const candidate = event.target.files[0]; if (!candidate) return;
      if (working || hashing || recoveryPending) { picker.value = ''; return; }
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(candidate.type) || candidate.size > 20 * 1024 * 1024) {
        picker.value = ''; inform('请选择20MB以内的JPG、PNG或WebP。源图未更换，仍使用' + (file?.name || expectedSource?.name || '展位试例') + '。', true); return;
      }
      if (!expectedSource && dirty && !window.confirm('更换源图会重置尺寸和结构填写。继续更换？')) { picker.value = ''; return; }
      hashing = true; recoveryLock(); inform('正在核对源图内容…');
      try {
        const sha256 = await digest(await candidate.arrayBuffer());
        if (!dialog.open) return;
        let restored = Boolean(expectedSource);
        if (expectedSource && sha256 !== expectedSource.sha256) {
          if (!window.confirm('这份文件不是草稿对应的原图。更换会清空旧尺寸、依据和结构填写，不能套用旧参数。仍要更换？')) {
            picker.value = ''; inform('这份文件与草稿源图不同。填写内容保留，请选择原文件；不会套用到这张图。', true); return;
          }
          restored = false;
        }
        file = candidate; fileProof = { kind: 'upload', sha256, name: file.name, size: file.size, type: file.type }; expectedSource = null;
        if (previewURL) URL.revokeObjectURL(previewURL);
        previewURL = URL.createObjectURL(file); image.src = previewURL; image.hidden = false; image.alt = '自选平面图'; original.href = previewURL; original.hidden = false;
        sourceInfo.textContent = '当前源图：' + file.name + '。以这张图填写尺寸和开口，不沿用展位参数。';
        if (!restored) spec = { ...floorplanDefaults(), width_mm: '', length_mm: '', height_mm: '', width_mm_source: '', length_mm_source: '',
          width_mm_kind: '', length_mm_kind: '', height_mm_source: '', open_sides: [], layout: 'empty' };
        spec.accept_assumption = false; changed(); paint();
        inform(restored ? '已核对同一份源图，填写内容已恢复。请重新确认层高假设与结构。'
          : '请填写净尺寸和层高依据，逐边核对开口。只准备单个矩形空间，不会自动识别房间和门窗。');
      } catch (error) { picker.value = ''; inform(error.message, true); }
      finally { hashing = false; recoveryLock(); }
    } });
  const controls = el('div', { class: 'zf-controls', tabindex: '0', role: 'region', 'aria-label': '尺寸与结构设置' });
  const viewNotice = el('p', { class: 'zd-notice zs-help', role: 'status', 'aria-live': 'polite',
    id: 'zyb-floorplan-view-notice', 'data-floorplan-view-notice': '1' });
  function refreshViewState() {
    const issue = floorplanViewIssue(spec);
    viewNotice.textContent = issue || (spec.view === 'A'
      ? 'A 是高于墙顶的外侧斜俯视，封闭空间会越墙俯看；若需室内视角，请选 C、D。'
      : spec.view === 'B' ? 'B 平视从上边开口外侧看向室内。开口必须与原图一致。'
      : 'C、D 机位在室内，不需要为视角新增整面开口。');
    viewNotice.dataset.error = String(Boolean(issue));
    submit.disabled = working || hashing || recoveryPending || submitted
      || exampleChecking && !file && !expectedSource || Boolean(issue);
  }
  const style = el('select', { 'aria-label': '选择空间风格', name: 'style_id', onchange: () => { dirty = true; persistDraft(); } },
    el('option', { value: '' }, '先只准备结构底稿'), packs.filter(pack => pack.available).map(pack => el('option', { value: pack.id }, pack.name + ' · ' + pack.kind)));
  style.value = packs.find(pack => pack.id === 'ink-gold' && pack.available)?.id || '';
  function field(label, key, numeric = false) {
    const input = el('input', { type: numeric ? 'number' : 'text', value: spec[key], required: true, name: key, 'aria-label': label,
      ...(numeric ? { min: key === 'height_mm' ? 1800 : 1000, max: key === 'height_mm' ? 8000 : 40000, step: 'any' } : { maxlength: 200 }),
      oninput: event => { spec[key] = numeric && event.target.value !== '' ? Number(event.target.value) : event.target.value; changed();
        if (key === 'height_mm' || key === 'height_mm_source') { spec.accept_assumption = false; confirmation.checked = false; } } });
    return el('label', { class: 'zs-field' }, el('span', {}, label), input);
  }
  function sourceKind(label, key) {
    const select = el('select', { name: key, required: true, 'aria-label': label,
      onchange: event => { spec[key] = event.target.value; changed(); } },
      el('option', { value: '' }, '请选择来源类型'),
      ['实测', '图纸', '产品资料', '设计目标'].map(value => el('option', { value }, value)));
    select.value = spec[key];
    return el('label', { class: 'zs-field' }, el('span', {}, label), select);
  }
  function paint() {
    const heightSource = sourceKind('已知层高来源类型', 'height_mm_kind');
    const heightSourceSelect = heightSource.querySelector('select');
    heightSource.hidden = spec.height_kind !== 'known';
    heightSourceSelect.disabled = spec.height_kind !== 'known';
    heightSourceSelect.required = spec.height_kind === 'known';
    confirmation.checked = spec.accept_assumption;
    confirmation.required = spec.height_kind === 'assumed';
    const heightKind = el('select', { name: 'height_kind', 'aria-label': '层高是否已知', onchange: event => {
      spec = switchHeightKind(spec, event.target.value); confirmation.required = spec.height_kind === 'assumed';
      heightSourceSelect.value = spec.height_mm_kind;
      controls.querySelector('[name=height_mm_source]').value = spec.height_mm_source;
      heightSource.hidden = spec.height_kind !== 'known';
      heightSourceSelect.disabled = spec.height_kind !== 'known';
      heightSourceSelect.required = spec.height_kind === 'known';
      changed(); spec.accept_assumption = false; confirmation.checked = false;
      confirmation.parentElement.hidden = spec.height_kind !== 'assumed';
      notice.dataset.error = 'false';
      notice.textContent = '层高状态已切换，数值保留。请重新填写对应的依据，并核对结构。';
    } }, el('option', { value: 'assumed' }, '试做假设（待确认）'), el('option', { value: 'known' }, '已知层高（填写依据）'));
    heightKind.value = spec.height_kind;
    const view = el('select', { name: 'view', 'aria-label': '出图视角', onchange: event => { spec.view = event.target.value; changed(); } },
      [['A', 'A · 入口斜俯视'], ['B', 'B · 入口平视'], ['C', 'C · 侧向空间'], ['D', 'D · 后区回望']].map(([value, label]) => el('option', { value }, label)));
    view.value = spec.view;
    const layout = el('select', { 'aria-label': '空间布置模板', onchange: event => { spec.layout = event.target.value; changed(); } },
      el('option', { value: 'booth' }, '佛事展位 · 近似布置'), el('option', { value: 'empty' }, '空矩形空间'));
    layout.value = spec.layout;
    controls.replaceChildren(el('h3', {}, '确认净尺寸与结构'), el('p', { class: 'zs-help' }, file || expectedSource
      ? '单位：毫米。填写原图内侧净尺寸；未填写的尺寸不会从图片推算。'
      : '单位：毫米。净尺寸与外轮廓分开；试例原方案外轮廓为7500 × 10000 mm。'),
      el('div', { class: 'zf-grid' }, field('净宽（mm）', 'width_mm', true), field('净长（mm）', 'length_mm', true)),
      el('div', { class: 'zf-grid' }, sourceKind('净宽来源类型', 'width_mm_kind'), sourceKind('净长来源类型', 'length_mm_kind')),
      field('净宽依据', 'width_mm_source'), field('净长依据', 'length_mm_source'),
      el('div', { class: 'zf-grid' }, field('层高（mm）', 'height_mm', true), el('label', { class: 'zs-field' }, el('span', {}, '层高状态'), heightKind)),
      heightSource,
      field('层高依据或假设', 'height_mm_source'), el('label', { class: 'zs-check' }, confirmation, el('span', {}, '确认用这个假设试做，不标作实测层高')),
      el('fieldset', { class: 'zf-open-sides' }, el('legend', {}, '整面开口（按原图方向）'), sides.map(([key, label]) => {
        const check = el('input', { type: 'checkbox', checked: spec.open_sides.includes(key), onchange: event => {
          spec.open_sides = event.target.checked ? [...spec.open_sides, key] : spec.open_sides.filter(v => v !== key);
          changed();
        } }); return el('label', { class: 'zs-check' }, check, el('span', {}, label));
      })), el('p', { class: 'zs-help' }, '未勾选的边按完整墙面处理；四边都不勾选表示封闭矩形空间。'),
      el('div', { class: 'zf-grid' }, el('label', { class: 'zs-field' }, el('span', {}, '空间布置'), layout), el('label', { class: 'zs-field' }, el('span', {}, '视角'), view)),
      viewNotice, el('p', { class: 'zs-help' }, '这些视角为固定试做机位，不是原图箭头的精确复现。'),
      el('label', { class: 'zs-field' }, el('span', {}, '风格方向'), style),
      el('label', { class: 'zs-check zf-structure-confirm' }, structureConfirmation, el('span', {}, '已核对当前原图、净尺寸、层高依据、开口边、布置与视角')),
      el('p', { class: 'zs-help' }, '准备后会保留原图，并生成平面复核图、结构灰模和深度底稿。确认参考设置后再出图；这一步不调用云端。'),
      el('p', { class: 'zs-help' }, '未提交内容仅暂存在当前标签页。关闭标签页后不保证保留；自选图需重新选择同一份文件，确认勾选不会恢复。'));
    confirmation.parentElement.hidden = spec.height_kind !== 'assumed';
  }
  const submit = el('button', { class: 'zs-btn primary', type: 'submit', 'aria-describedby': 'zyb-floorplan-view-notice' }, '准备结构底稿');
  const saved = el('section', { class: 'zf-saved', role: 'region', 'aria-label': '已保存试例续接', hidden: true });
  function markSaved(value) {
    const source = fileProof || expectedSource || { kind: 'example', sha256: exampleSha };
    if (!floorplanReceiptValid(value, source.sha256)) throw Error('已保存试例的来源记录无效，请从我的设计中核对。');
    receipt = structuredClone(value); dirty = false; submitted = true; submit.textContent = '底稿已保存';
    saved.replaceChildren(el('p', { class: 'zs-help' }, '原图和三份结构底稿已保存。继续操作只核对并打开这份试例，不重新上传或准备底稿。'),
      el('button', { type: 'button', class: 'zs-btn primary', onclick: resumeSaved }, '核对并继续'),
      el('a', { class: 'zs-btn quiet', href: '/?zyb=studio&at=design/' + receipt.task_id + '/images' }, '直接打开已保存试例'));
    saved.hidden = false; const result = persistDraft(); recoveryLock(); return result;
  }
  function finishSaved(result) {
    if (!result?.opened) { inform('底稿已保存。' + (result?.message || '暂时无法打开，请稍后核对并继续。'), true); return; }
    if (result.stylePending) {
      persistDraft(); inform('试例已打开。' + result.stylePending + ' 续接记录已保留。', true);
      if (!saved.querySelector('[data-style-review]')) saved.append(el('button', { type: 'button', class: 'zs-btn quiet',
        'data-style-review': '', onclick: () => dialog.close() }, '转到试例核对风格'));
      return;
    }
    const cleared = drafts.clear();
    if (!cleared.ok) { storageError = cleared.error + ' 底稿已保存，旧续接记录可能仍会出现。'; inform('试例已打开，请核对设计内容。', true); }
    else { completed?.(); dialog.close(); }
  }
  async function resumeSaved() {
    if (working || !receipt || typeof continueSaved !== 'function') return;
    working = true; recoveryLock(); inform('正在核对已保存原图与底稿…');
    try { finishSaved(await continueSaved({ receipt: structuredClone(receipt), styleId: style.value })); }
    catch (error) { inform('底稿已保存。' + error.message, true); }
    finally { working = false; if (dialog.open) recoveryLock(); }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (working || hashing || recoveryPending || submitted) return;
    const viewIssue = floorplanViewIssue(spec);
    if (viewIssue) { inform(viewIssue + ' 未创建试例。', true); controls.querySelector('[name=view]').focus(); return; }
    if (expectedSource && !file) { inform('请先重新选择草稿对应的原文件，不能以展位试例代替。', true); picker.focus(); return; }
    const sourceSha256 = fileProof?.sha256 || exampleSha;
    if (!sourceSha256) { inform('当前源图尚未校验，未创建试例。请重新选择源图或稍后重开。', true); return; }
    if (!form.reportValidity()) return;
    for (const key of ['width_mm_source', 'length_mm_source', 'height_mm_source']) {
      if (!String(spec[key] || '').trim()) {
        inform('尺寸依据不能只填空格。未创建试例，请补齐依据。', true); controls.querySelector('[name=' + key + ']').focus(); return;
      }
    }
    if (spec.layout === 'booth' && (spec.width_mm < 5500 || spec.length_mm < 7000)) {
      inform('展位布置模板需至少5.5×7米，小空间请选空矩形模板。未创建试例。', true); return;
    }
    working = true; submit.disabled = true; notice.textContent = '正在保存原图并准备结构底稿…';
    dialog.querySelectorAll('input,select,button').forEach(input => { input.disabled = true; });
    try {
      const result = await prepare({ spec: structuredClone(spec), file, sourceKind: file ? 'upload' : 'example', sourceSha256,
        style: packs.find(pack => pack.id === style.value), onSaved: markSaved });
      finishSaved(result);
    }
    catch (error) { persistDraft(); inform((receipt ? '底稿已保存。' : '') + error.message, true); }
    finally {
      working = false; recoveryLock();
    }
  });
  form.append(el('div', { class: 'zf-body' }, el('section', { class: 'zf-source', tabindex: '0', 'aria-label': '平面图原图' }, image,
    sourceInfo,
    el('div', { class: 'zs-row' }, original, el('label', { class: 'zs-btn quiet zf-picker' }, picker, '换成自己的平面图'),
      el('button', { class: 'zs-btn quiet', type: 'button', onclick: () => {
        if (working || hashing || recoveryPending || dirty && !window.confirm('使用展位试例会重置当前填写。继续？')) return;
        file = null; fileProof = null; expectedSource = null; picker.value = ''; if (previewURL) URL.revokeObjectURL(previewURL); previewURL = null;
        image.src = '/zyb/studio/floorplan-example'; image.hidden = false; image.alt = '客户原始展位尺寸图'; original.href = image.src; original.hidden = false;
        sourceInfo.textContent = '真实试例：2026年春季佛事展。原图和原方案页均保留在资料目录。';
        spec = floorplanDefaults(); changed(); paint(); notice.textContent = '已切换到展位试例，请核对尺寸与结构。'; notice.dataset.error = 'false';
      } }, '使用展位试例'))), controls),
    el('footer', { class: 'zt-footer' }, notice, saved, submit));
  const requestClose = () => {
    if (working || hashing) return;
    if (!recoveryPending && dirty) {
      persistDraft();
      if (!window.confirm(storageError ? '浏览器无法暂存这次填写。关闭会丢失未提交内容，仍然关闭？'
        : '填写尚未提交。关闭后保留本标签页草稿，下次打开可恢复；关闭标签页后不保证保留。现在关闭？')) return;
    }
    dialog.close();
  };
  const recovery = el('section', { class: 'zt-draft-recovery', role: 'region', 'aria-label': '平面图草稿恢复', hidden: !recoveryPending });
  const exampleProof = (async () => {
    try {
      const response = await fetch('/zyb/studio/floorplan-example');
      if (!response.ok) throw Error('试例源图暂时无法校验。');
      exampleSha = await digest(await response.arrayBuffer());
      if (dialog.open && dirty && !file && !expectedSource && !recoveryPending) { persistDraft(); inform('填写尚未提交，本标签页草稿已暂存。'); }
      return exampleSha;
    } catch (error) { if (dialog.open && !file && !expectedSource) inform(error.message, true); return null; }
    finally { exampleChecking = false; if (dialog.open) recoveryLock(); }
  })();
  if (recoveryPending) recovery.append(el('p', { class: 'zs-help' }, !recovered.ok ? recovered.error + ' 可以清除它后重新填写。'
    : recovered.value.receipt ? '找到已保存试例的续接记录。将核对服务器上的原图与底稿，不重新上传或准备底稿。'
    : recovered.value.source.kind === 'upload' ? '找到自选平面图的未提交草稿。恢复后需重新选择并核对同一份原文件，确认勾选不会恢复。'
      : '找到展位试例的未提交草稿。恢复后请重新核对结构和层高依据；确认勾选不会恢复。'),
    ...(recovered.ok ? [el('button', { type: 'button', class: 'zs-btn primary', onclick: async () => {
      if (working || hashing) return;
      if (recovered.value.receipt) {
        spec = structuredClone(recovered.value.spec); style.value = recovered.value.style;
        if (recovered.value.style && !packs.some(pack => pack.available && pack.id === recovered.value.style)) {
          style.append(el('option', { value: recovered.value.style }, '原风格暂不可用')); style.value = recovered.value.style;
        }
        fileProof = structuredClone(recovered.value.source); file = null; expectedSource = null;
        recoveryPending = false; recovery.hidden = true; paint();
        image.src = '/zyb/studio/asset/' + recovered.value.receipt.task_id + '/' + recovered.value.receipt.source_id;
        image.alt = '已保存平面图'; original.href = image.src;
        sourceInfo.textContent = '已保存试例的源图。续接前会核对图像内容和底稿版本。';
        markSaved(recovered.value.receipt); await resumeSaved(); return;
      }
      if (recovered.value.source.kind === 'example' && recovered.value.source.sha256 !== await exampleProof) {
        inform('展位试例源图已变化或无法核对。未恢复旧参数，请重新填写。', true); return;
      }
      if (!dialog.open) return;
      spec = { ...structuredClone(recovered.value.spec), accept_assumption: false }; structureConfirmation.checked = false;
      const hasStyle = !recovered.value.style || packs.some(pack => pack.available && pack.id === recovered.value.style);
      style.value = hasStyle ? recovered.value.style : '';
      if (recovered.value.source.kind === 'upload') {
        expectedSource = structuredClone(recovered.value.source); file = null; fileProof = null; image.hidden = true; original.hidden = true;
        sourceInfo.textContent = '草稿源图：' + expectedSource.name + '。请重新选择同一份原文件；不会使用展位试例替代。';
      }
      recoveryPending = false; recovery.hidden = true; dirty = true; paint(); recoveryLock(); persistDraft();
      inform((expectedSource ? '填写已恢复，请重新选择对应原文件。' : '填写已恢复，尚未提交。')
        + ' 请重新核对结构和层高假设。' + (hasStyle ? '' : '原风格不可用，当前只准备结构底稿。'));
    } }, recovered.value.receipt ? '核对已保存试例并继续' : '恢复平面图草稿')] : []),
    el('button', { type: 'button', class: 'zs-btn quiet', onclick: () => {
      const result = drafts.clear(); storageError = result.ok ? '' : result.error + ' 旧草稿可能仍会出现。';
      recoveryPending = false; recovery.hidden = true; recoveryLock();
      inform(result.ok ? recovered.value?.receipt ? '已清除本标签页续接记录，已保存试例仍在我的设计中。' : '已放弃本标签页草稿，请重新填写。'
        : '本次不恢复记录，但浏览器未能清除它。', !result.ok);
    } }, recovered.value?.receipt ? '清除续接记录，保留试例' : '放弃平面图草稿'));
  dialog.append(el('header', { class: 'zt-head' }, el('div', {}, el('small', {}, '原图 → 尺寸确认 → 结构底稿 → 效果图'), el('h2', {}, '平面图试做')),
    el('button', { type: 'button', class: 'zs-btn quiet', onclick: requestClose }, '关闭')), recovery, form);
  dialog.addEventListener('cancel', event => { event.preventDefault(); requestClose(); });
  dialog.addEventListener('close', () => { if (previewURL) URL.revokeObjectURL(previewURL); dialog.remove(); }, { once: true });
  const beforeUnload = event => {
    if (!dirty || recoveryPending) return;
    persistDraft(); event.preventDefault(); event.returnValue = '';
  };
  const pageHide = () => { if ((dirty || receipt) && !recoveryPending) persistDraft(); };
  window.addEventListener('beforeunload', beforeUnload); window.addEventListener('pagehide', pageHide);
  dialog.addEventListener('close', () => {
    window.removeEventListener('beforeunload', beforeUnload); window.removeEventListener('pagehide', pageHide);
  }, { once: true });
  document.body.append(dialog); paint(); dialog.showModal(); recoveryLock(); return dialog;
}
