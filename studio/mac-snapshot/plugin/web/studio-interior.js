import { interiorDraftStore } from './studio-interior-drafts.js?v=3';

const imageTypes = '.jpg,.jpeg,.png,.webp';
let entryDraft = null;
let entryStorage = null, unloadGuardInstalled = false;
const presets = [
  ['自然写实', '自然材质、真实照明，避免夸张装饰。'],
  ['现代简约', '简洁线条、留白和协调的材质比例。'],
  ['现代木作', '温润木材、细致木作和自然光。'],
  ['现代轻奢', '石材、木作与少量金属，精细收口和有层次的照明。'],
  ['中式雅致', '克制的中式木作、自然石材和协调的装饰。'],
];
export function interiorStyles(packs = [], current = null) {
  const choices = [
    ...presets.map(([name, description]) => ({ id: 'preset:' + name, name, description, references: [] })),
    ...packs.filter(pack => pack.available).map(pack => ({ ...pack, id: 'pack:' + pack.id })),
  ];
  if (current?.style_label && !choices.some(style => style.name === current.style_label))
    choices.push({ id: 'current', name: current.style_label, description: current.style_description || '', references: [] });
  return choices;
}
const interiorSupplementTakes = {
  style: '只参考风格、配色和照明。',
  material: '只参考材质颜色与纹理。',
  object: '只参考家具或陈设造型。',
};
const interiorGeneratedTakes = new Set([
  ...Object.values(interiorSupplementTakes),
  '只参考配色、材质、装饰层次和照明，不照搬场景布局。',
  '只参考材质搭配、装饰层次和光线，不照搬人物或宗教细节。',
]);
export function setInteriorReferenceUse(row, use) {
  if (interiorGeneratedTakes.has(row.take))
    row.take = interiorSupplementTakes[use] || '';
  row.use = use;
}
export function interiorSupplementReference(item, id) {
  const target = (item.target || '').trim();
  return { source: 'upload', id, use: item.use, fidelity: 'reference',
    take: interiorSupplementTakes[item.use] || interiorSupplementTakes.style,
    ...(target ? { target } : {}) };
}
export function interiorPreparationIdentity(style, extras, hashes) {
  return { sources: [...hashes], uses: extras.map(item => item.use), targets: extras.map(item => (item.target || '').trim()),
    style: { id: style.id, name: style.name, description: style.description || '' },
    styles: style.references.map(row => row.id) };
}
function sameInteriorDraft(left, right) {
  return Boolean(left && right && left.plan === right.plan && left.styleId === right.styleId
    && left.styleName === right.styleName && left.goal === right.goal && left.extras.length === right.extras.length
    && left.extras.every((row, index) => row.file === right.extras[index].file && row.use === right.extras[index].use
      && (row.target || '') === (right.extras[index].target || '')));
}
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'class') node.className = value;
    else if (['disabled', 'hidden'].includes(key)) node[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.flat().filter(value => value != null));
  return node;
}
export function interiorIssue(record, rows) {
  if (!record || record.schema !== 1 || !/^[a-f0-9]{32}$/.test(record.source_id || '') || !/^[a-f0-9]{64}$/.test(record.source_sha256 || ''))
    return '室内设计的平面图记录不完整，请重新核对来源。';
  const selected = rows.filter(row => row.source === 'upload' && row.id === record.source_id);
  if (selected.length !== 1) return '请保留一份原始平面图，不能移除或重复选用。';
  if (selected[0].use !== 'reference' || selected[0].source_selection || (selected[0].fidelity || 'reference') !== 'reference')
    return '平面图仅用于完整空间布局，请保留完整图片并将用途设为参考。';
  return '';
}
export function interiorUnresolvedStates(task, conceptIds) {
  const selected = new Set(conceptIds), states = new Map();
  const priority = { running: 1, uncertain: 2, 'registration-pending': 3 };
  const timestamp = value => typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value)) ? Date.parse(value) : null;
  const created = new Map((task.concepts || []).map(row => [row.id, timestamp(row.created)]));
  const history = task.cloud_job_history || [];
  if (!Array.isArray(history)) return new Map([...selected].map(id => [id, 'uncertain']));
  for (const job of [task.job || {}, ...history]) {
    if (!job || typeof job !== 'object' || Array.isArray(job) || job.items && (typeof job.items !== 'object' || Array.isArray(job.items))) {
      selected.forEach(id => states.set(id, 'uncertain')); continue;
    }
    const items = job.items || {};
    for (const [id, state] of Object.entries(items))
      if (selected.has(id) && Object.hasOwn(priority, state) && priority[state] > (priority[states.get(id)] || 0)) states.set(id, state);
    if (job.action === 'design' && job.status === 'uncertain' && !Object.keys(items).length) {
      const hasScope = Object.hasOwn(job, 'concept_ids');
      const scoped = hasScope && Array.isArray(job.concept_ids) && job.concept_ids.every(id => typeof id === 'string');
      if (hasScope && !scoped) { selected.forEach(id => states.set(id, 'uncertain')); continue; }
      const cutoffs = ['finished', 'retained_at', 'continued_at'].map(key => timestamp(job[key])).filter(value => value !== null);
      const cutoff = cutoffs.length ? Math.min(...cutoffs) : null;
      selected.forEach(id => {
        const affected = scoped ? job.concept_ids.includes(id) : cutoff === null || created.get(id) == null || created.get(id) <= cutoff;
        if (affected && !states.has(id)) states.set(id, 'uncertain');
      });
    }
  }
  return states;
}
export function interiorBatchProgress(task, batchId) {
  const concepts = (task.concepts || []).filter(row => row.batch === batchId && row.reference_design
    && row.interior_photographic && row.interior_view && typeof row.interior_view === 'object');
  const completedIds = new Set((task.images || []).map(row => row.concept_id));
  const unresolvedIds = interiorUnresolvedStates(task, concepts.map(row => row.id));
  const completed = concepts.filter(row => completedIds.has(row.id));
  const missing = concepts.filter(row => !completedIds.has(row.id));
  return { completed, missing, resumable: missing.filter(row => !unresolvedIds.has(row.id)),
    unresolved: missing.filter(row => unresolvedIds.has(row.id)), style: concepts[0]?.interior_style_label || '' };
}
export function interiorSpatialGroups(concepts, images = []) {
  const groups = new Map(), completed = new Map(images.filter(Boolean).map(image => [image.concept_id, image]));
  const point = value => Array.isArray(value) && value.length === 2
    && value.every(axis => Number.isFinite(axis) && axis >= 0 && axis <= 1);
  for (const concept of concepts) {
    const binding = concept?.interior_view, layout = binding?.layout_snapshot;
    if (!binding || !layout || !/^[a-f0-9]{32}$/.test(binding.layout_id || '')
      || !/^[a-f0-9]{64}$/.test(binding.layout_sha256 || '')
      || layout.coordinate_frame !== 'detail_image' || !Array.isArray(layout.spaces) || !layout.spaces.length
      || !Array.isArray(layout.coordinate_pixels) || layout.coordinate_pixels.length !== 2
      || !layout.coordinate_pixels.every(axis => Number.isFinite(axis) && axis > 0)
      || !layout.spaces.every(room => room && typeof room.id === 'string' && Array.isArray(room.outline)
        && room.outline.length >= 3 && room.outline.length <= 512 && room.outline.every(point))
      || new Set(layout.spaces.map(room => room.id)).size !== layout.spaces.length
      || !layout.spaces.some(room => room.id === binding.space_id)) continue;
    const key = binding.layout_id + ':' + binding.layout_sha256;
    const serialized = JSON.stringify(layout), group = groups.get(key);
    if (group && group.snapshot !== serialized) { group.invalid = true; continue; }
    if (!group) groups.set(key, { key, layoutId: binding.layout_id, layoutHash: binding.layout_sha256,
      layout, snapshot: serialized, unmapped: 0, rooms: layout.spaces.map(room => ({ ...room, concepts: [], completed: 0 })) });
    const room = groups.get(key).rooms.find(room => room.id === binding.space_id);
    room.concepts.push(concept);
    const photo = completed.get(concept.id), registered = photo?.floorplan_input;
    const matches = !registered || typeof registered === 'object' && !Array.isArray(registered)
      && ['layout_id', 'layout_sha256', 'space_id', 'view_id'].every(field =>
        !Object.hasOwn(registered, field) || registered[field] === binding[field]);
    if (photo && matches) room.completed++;
    else if (photo) groups.get(key).unmapped++;
  }
  return [...groups.values()].filter(group => !group.invalid);
}
export function interiorSpatialOverview({ concepts, images, selectRoom, sourceForLayout, disclosureId }) {
  const groups = interiorSpatialGroups(concepts, images);
  if (!groups.length) return null;
  const panels = groups.map((group, index) => {
    const [width, height] = group.layout.coordinate_pixels;
    const points = group.rooms.flatMap(room => room.outline);
    const left = Math.min(...points.map(point => point[0])) * width;
    const top = Math.min(...points.map(point => point[1])) * height;
    const right = Math.max(...points.map(point => point[0])) * width;
    const bottom = Math.max(...points.map(point => point[1])) * height;
    if (right <= left || bottom <= top) return null;
    const pad = Math.max(right - left, bottom - top) * .035;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `${left - pad} ${top - pad} ${right - left + pad * 2} ${bottom - top + pad * 2}`);
    svg.setAttribute('class', 'zs-space-map');
    svg.setAttribute('role', 'group'); svg.setAttribute('aria-label', '本轮房间位置示意');
    const controls = [];
    const focusPrefix = `${disclosureId || 'interior-space-map'}-${group.key}`;
    function choose(room) {
      for (const control of controls) control.setAttribute('aria-pressed', String(control.dataset.room === room.id));
      selectRoom?.(room.id, group.layoutId, group.layoutHash);
    }
    const buttons = group.rooms.map((room, roomIndex) => {
      const count = room.concepts.length ? `${room.completed}/${room.concepts.length} 个视角` : '未安排视角';
      const label = typeof room.label === 'string' && room.label.trim() ? room.label : `空间 ${roomIndex + 1}`;
      const polygon = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
      polygon.setAttribute('points', room.outline.map(([x, y]) => `${x * width},${y * height}`).join(' '));
      polygon.setAttribute('data-room', room.id); polygon.setAttribute('data-completed', String(room.completed > 0));
      polygon.setAttribute('role', 'button'); polygon.setAttribute('tabindex', '0');
      polygon.setAttribute('data-focus', `${focusPrefix}-${encodeURIComponent(room.id)}-map`);
      polygon.setAttribute('aria-label', `${label}，${count}`); polygon.setAttribute('aria-pressed', 'false');
      polygon.addEventListener('click', () => choose(room));
      polygon.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); choose(room); }
      });
      svg.append(polygon);
      const button = el('button', { type: 'button', class: 'zs-btn quiet small', 'data-room': room.id,
        'data-focus': `${focusPrefix}-${encodeURIComponent(room.id)}-button`,
        'aria-pressed': 'false', onclick: () => choose(room) },
        el('span', {}, label), el('span', { class: 'zs-space-count' }, count));
      controls.push(polygon, button);
      return button;
    });
    const source = sourceForLayout?.(group.layoutId, group.layoutHash);
    return el('section', { class: 'zs-space-overview', 'aria-label': groups.length > 1 ? `空间分布 ${index + 1}` : '空间分布' },
      el('div', { class: 'zs-row' }, el('p', { class: 'zs-help' },
        `${group.rooms.filter(room => room.completed).length}/${group.rooms.length} 个空间已有结果 · 点房间查看视角`),
        source ? el('a', { class: 'zs-btn quiet small', href: source, target: '_blank', rel: 'noopener',
          'data-focus': focusPrefix + '-source' }, '查看本轮原始平面图') : null),
      el('div', { class: 'zs-space-overview-body' }, svg,
        el('div', { class: 'zs-space-buttons', 'aria-label': '选择房间' }, buttons)),
      el('p', { class: 'zs-help zs-space-caption' }, '房间位置示意'),
      group.unmapped ? el('p', { class: 'zs-help' }, `${group.unmapped} 张图片可在下方查看，未加入位置统计。`) : null);
  }).filter(Boolean);
  if (!panels.length) return null;
  return el('details', { class: 'zs-details zs-space-distribution', ...(disclosureId ? { 'data-disclosure': disclosureId } : {}) },
    el('summary', { ...(disclosureId ? { 'data-focus': disclosureId + '-summary' } : {}) }, '查看空间分布'), panels);
}
export function openInterior({ packs, prepare, validateFile, storage = entryStorage ||= interiorDraftStore() }) {
  let plan = entryDraft?.plan || null, extras = [], working = false, prepared = false, restoring = !entryDraft, saveSerial = 0;
  if (!unloadGuardInstalled && globalThis.addEventListener) {
    globalThis.addEventListener('beforeunload', event => {
      if (entryDraft && (entryDraft.plan || entryDraft.goal || entryDraft.extras.length)
        && entryStorage && (entryStorage.pending || entryStorage.error)) { event.preventDefault(); event.returnValue = ''; }
    });
    unloadGuardInstalled = true;
  }
  const urls = new Set();
  const preview = file => { const url = URL.createObjectURL(file); urls.add(url); return url; };
  const notice = el('p', { class: 'zd-notice', role: 'status' });
  const draftNotice = el('p', { class: 'zs-help', role: 'status', 'data-interior-draft': '1' });
  const dialog = el('dialog', { class: 'zs-tool-dialog zf-dialog', 'aria-label': '平面图生成室内实景' });
  const original = el('img', { class: 'zf-source-image', alt: '所选原始平面图', hidden: true });
  const zoom = el('a', { class: 'zs-btn quiet small', target: '_blank', rel: 'noopener', hidden: true }, '放大查看原图');
  const legibility = el('p', { class: 'zs-help', role: 'status', hidden: true });
  original.addEventListener('load', () => {
    legibility.hidden = false;
    legibility.textContent = `${original.naturalWidth} × ${original.naturalHeight} 像素。`
      + (Math.max(original.naturalWidth, original.naturalHeight) < 1000 ? '使用更清晰的原图，有助于识别图中的细节。' : '');
  });
  const planLabel = el('p', { class: 'zs-help' }, '上传 JPG、PNG 或 WebP 平面图。PDF、CAD 文件请先导出清晰图片。');
  if (plan) {
    original.src = preview(plan); original.hidden = false;
    zoom.href = original.src; zoom.hidden = false; planLabel.textContent = plan.name;
  }
  const choosePlan = el('input', { type: 'file', accept: imageTypes, 'aria-label': '上传室内平面图', onchange: event => {
    try {
      const file = event.target.files[0]; if (!file) return;
      validateFile(file); plan = file; original.src = preview(file); original.hidden = false;
      zoom.href = original.src; zoom.hidden = false;
      planLabel.textContent = file.name; notice.textContent = ''; refresh();
    } catch (error) { event.target.value = ''; notice.textContent = error.message; notice.dataset.error = 'true'; }
  } });
  function reflectPlanFile() {
    if (!plan) return;
    try { const transfer = new DataTransfer(); transfer.items.add(plan); choosePlan.files = transfer.files; } catch {}
  }
  reflectPlanFile();
  const choices = interiorStyles(packs);
  const styles = el('select', { name: 'interior_style', 'aria-label': '选择室内风格', onchange: () => { paintStyle(); refresh(); } },
    choices.map(style => el('option', { value: style.id }, style.name + (style.references.length ? ' · 精选参考' : ''))));
  if (entryDraft) {
    if (!choices.some(style => style.id === entryDraft.styleId)) styles.append(el('option', { value: entryDraft.styleId, disabled: true }, entryDraft.styleName + '（暂不可用）'));
    styles.value = entryDraft.styleId;
  }
  const styleNote = el('p', { class: 'zs-help' });
  const stylePhotos = el('div', { class: 'zt-tool-previews', 'aria-label': '所选风格参考' });
  const selectedStyle = () => choices.find(style => style.id === styles.value);
  function paintStyle() {
    const style = selectedStyle();
    styleNote.textContent = style ? [style.kind, style.description, style.caution].filter(Boolean).join(' · ') : '上次的风格参考暂不可用，请选择其他风格。';
    stylePhotos.replaceChildren(...(style?.references || []).map(row => el('figure', {},
      el('img', { src: '/zyb/studio/reference-image/' + row.id, alt: row.title }),
      el('figcaption', { class: 'zs-help' }, row.credit || '', row.rights ? ' · ' + row.rights : '',
        row.source_url ? el('a', { href: row.source_url, target: '_blank', rel: 'noopener' }, ' 来源') : null))));
  }
  const extraList = el('div', { class: 'zs-saved-assets' });
  function paintExtras() {
    extraList.replaceChildren(...extras.map((item, index) => {
      const purpose = el('select', { 'aria-label': item.file.name + ' 的参考用途', onchange: event => { item.use = event.target.value; } },
        [['style', '风格与氛围'], ['material', '材质颜色与纹理'], ['object', '家具或陈设造型']].map(([value, label]) => el('option', { value }, label)));
      purpose.value = item.use;
      const target = el('input', { type: 'text', maxlength: 160, 'aria-label': item.file.name + ' 的应用位置（可选）',
        placeholder: '如：客厅地板、卧室床头', oninput: event => { item.target = event.target.value; } });
      target.value = item.target || '';
      return el('div', { class: 'zf-extra' }, el('img', { src: item.preview, alt: '', width: 72, height: 54 }),
        el('div', { class: 'zf-extra-fields' }, el('span', { class: 'zf-extra-name' }, item.file.name), purpose,
          el('label', { class: 'zs-field' }, el('span', {}, '应用位置（可选）'), target)),
        el('button', { type: 'button', class: 'zs-btn quiet small', 'aria-label': '移除 ' + item.file.name,
          onclick: () => { extras.splice(index, 1); paintExtras(); refresh(); rememberDraft(); } }, '移除'));
    }));
  }
  const addPhotos = el('input', { type: 'file', accept: imageTypes, multiple: true, 'aria-label': '上传补充参考图片', onchange: event => {
    try {
      const files = [...event.target.files]; files.forEach(validateFile);
      if (1 + (selectedStyle()?.references.length || 0) + extras.length + files.length > 10) throw Error('平面图、所选风格与补充照片合计最多10张，请减少图片。');
      extras.push(...files.map(file => ({ file, use: 'style', target: '', preview: preview(file) })));
      paintExtras(); refresh(); notice.textContent = '';
    } catch (error) { notice.textContent = error.message; notice.dataset.error = 'true'; }
    event.target.value = '';
  } });
  const goal = el('textarea', { name: 'interior_goal', rows: 3, maxlength: 600, 'aria-label': '补充要求',
    placeholder: '可选：比如更温暖一些、增加木作，或重点展示某个空间。不填也可以直接生成。' });
  goal.value = entryDraft?.goal || '';
  extras = (entryDraft?.extras || []).map(item => ({ ...item, preview: preview(item.file) }));
  const submit = el('button', { type: 'submit', class: 'zs-btn primary', disabled: true }, '生成室内效果');
  const prepareOnly = el('button', { type: 'submit', class: 'zs-btn quiet', 'data-prepare-only': '1', disabled: true }, '保存草稿');
  const continueSaved = el('button', { type: 'button', class: 'zs-btn primary', hidden: true, onclick: () => dialog.close() }, '查看已保存的设计');
  const budget = el('p', { class: 'zs-help', role: 'status' });
  function refresh() {
    const style = selectedStyle(), count = 1 + (style?.references.length || 0) + extras.length;
    budget.textContent = !style ? '请选择可用的室内风格。' : !plan ? '选择平面图后即可生成。' : count > 10 ? `目前有 ${count} 张图片，请移除 ${count - 10} 张后生成。`
      : `已选平面图${style.references.length ? '、风格参考 ' + style.references.length + ' 张' : ''}${extras.length ? '、补充照片 ' + extras.length + ' 张' : ''}。`;
    submit.disabled = prepared || working || restoring || !plan || !style || count > 10;
    prepareOnly.disabled = submit.disabled;
  }
  const snapshot = () => ({ plan, styleId: styles.value, styleName: selectedStyle()?.name || entryDraft?.styleName || '',
    goal: goal.value, extras: extras.map(({ file, use, target }) => ({ file, use, target: target || '' })) });
  function rememberDraft() {
    if (restoring || working || prepared) return;
    const nextDraft = snapshot();
    if (!storage.error && sameInteriorDraft(entryDraft, nextDraft)) return;
    entryDraft = nextDraft;
    const serial = ++saveSerial;
    draftNotice.textContent = '正在暂存所选内容…';
    storage.write(entryDraft).then(result => {
      if (serial !== saveSerial || !dialog.isConnected || prepared) return;
      draftNotice.textContent = result.ok ? '已在这台设备暂存，刷新后可继续。' : result.error;
      draftNotice.dataset.error = String(!result.ok);
    });
  }
  const form = el('form', { class: 'zf-form', onsubmit: async event => {
    event.preventDefault(); if (working || !plan || submit.disabled) return;
    working = true; dialog.querySelectorAll('input,select,textarea,button').forEach(node => { node.disabled = true; });
    notice.dataset.error = 'false'; notice.textContent = '正在保存原始平面图和所选参考…';
    try { await prepare({ plan, extras, style: selectedStyle(), goal: goal.value.trim(), startGeneration: event.submitter?.dataset.prepareOnly !== '1',
      reportProgress: message => { notice.textContent = message; } });
      prepared = true; entryDraft = null;
      draftNotice.textContent = '';
      let cleared;
      try { cleared = await (storage.complete ? storage.complete() : storage.clear()); } catch {}
      if (cleared?.ok) dialog.close();
      else {
        notice.textContent = '设计已保存，可以继续查看。浏览器暂时未能清理旧选择，刷新后可能再次显示。';
        submit.hidden = prepareOnly.hidden = true; continueSaved.hidden = false;
      }
    }
    catch (error) { notice.textContent = error.message; notice.dataset.error = 'true'; }
    finally {
      working = false;
      dialog.querySelectorAll('button').forEach(node => { node.disabled = false; });
      dialog.querySelectorAll('input,select,textarea').forEach(node => { node.disabled = prepared; }); refresh();
      if (prepared && dialog.isConnected && !continueSaved.hidden) continueSaved.focus();
    }
  } }, el('div', { class: 'zf-body' },
    el('section', { class: 'zf-source' }, el('h3', {}, '平面图'), choosePlan, planLabel, original, zoom, legibility),
    el('section', { class: 'zf-controls' }, el('h3', {}, '室内风格'), styles, styleNote, stylePhotos,
      el('details', { class: 'zs-details' }, el('summary', {}, '添加参考图片和要求（可选）'),
        el('p', { class: 'zs-help' }, '可以添加喜欢的风格、材质或家具照片。'), addPhotos, extraList,
        el('label', { class: 'zs-field' }, el('span', {}, '补充要求（可选）'), goal)), budget, draftNotice)),
    el('footer', { class: 'zt-footer' }, notice, submit, prepareOnly, continueSaved,
      el('p', { class: 'zs-help' }, '原始平面图和生成结果都会保存，之后可以继续调整。')));
  dialog.append(el('header', { class: 'zt-head' }, el('div', {}, el('small', {}, '上传平面图，选择喜欢的风格'), el('h2', {}, '生成室内实景')),
    el('button', { type: 'button', class: 'zs-btn quiet', onclick: () => { if (!working) dialog.close(); } }, '关闭')), form);
  dialog.addEventListener('cancel', event => { if (working) event.preventDefault(); });
  form.addEventListener('input', rememberDraft);
  form.addEventListener('change', rememberDraft);
  dialog.addEventListener('close', () => {
    urls.forEach(url => URL.revokeObjectURL(url)); dialog.remove();
  }, { once: true });
  if (entryDraft?.plan) notice.textContent = '上次选择的图片和要求还在，可以继续。';
  if (entryDraft) draftNotice.textContent = storage.pending ? '正在暂存所选内容…' : storage.error || '已在这台设备暂存，刷新后可继续。';
  dialog.querySelector('details').open = Boolean(extras.length || goal.value);
  paintStyle(); paintExtras(); refresh(); document.body.append(dialog); dialog.showModal();
  const toggleInputs = disabled => dialog.querySelectorAll('input,select,textarea').forEach(node => { node.disabled = disabled; });
  toggleInputs(restoring);
  if (restoring) {
    draftNotice.textContent = '正在恢复上次选择…';
    storage.read().then(result => {
      if (!dialog.isConnected) return;
      try {
        if (!result.ok) { draftNotice.textContent = result.error; return; }
        if (!result.value) { draftNotice.textContent = ''; return; }
        const saved = result.value;
        [saved.plan, ...saved.extras.map(row => row.file)].filter(Boolean).forEach(validateFile);
        entryDraft = saved; plan = saved.plan; reflectPlanFile(); goal.value = saved.goal;
        if (!choices.some(style => style.id === saved.styleId)) styles.append(el('option', { value: saved.styleId, disabled: true }, saved.styleName + '（暂不可用）'));
        styles.value = saved.styleId;
        extras = saved.extras.map(row => ({ ...row, preview: preview(row.file) }));
        if (plan) { original.src = preview(plan); original.hidden = false; zoom.href = original.src; zoom.hidden = false; planLabel.textContent = plan.name; }
        paintStyle(); paintExtras(); dialog.querySelector('details').open = Boolean(extras.length || goal.value);
        draftNotice.textContent = '已恢复上次选择的图片、风格和要求。';
      } catch { draftNotice.textContent = '旧草稿暂时无法恢复，浏览器中的记录没有改动。'; }
      finally { restoring = false; toggleInputs(false); refresh(); }
    });
  }
  return dialog;
}
export function openInteriorStyle({ packs, current, apply }) {
  const choices = interiorStyles(packs, current);
  let working = false;
  const notice = el('p', { class: 'zd-notice', role: 'status' });
  const dialog = el('dialog', { class: 'zs-tool-dialog', 'aria-label': '更换室内风格' });
  const note = el('p', { class: 'zs-help' });
  const select = el('select', { 'aria-label': '新的室内风格', onchange: () => {
    note.textContent = choices.find(style => style.id === select.value).description;
  } }, choices.map(style => el('option', { value: style.id }, style.name)));
  select.value = choices.find(style => style.name === current.style_label)?.id || choices[0].id;
  note.textContent = choices.find(style => style.id === select.value).description;
  const form = el('form', { onsubmit: async event => {
    event.preventDefault(); if (working) return;
    const generate = event.submitter?.dataset.saveOnly !== '1';
    working = true; select.disabled = true;
    dialog.querySelectorAll('button').forEach(node => { node.disabled = true; });
    notice.textContent = generate ? '正在应用风格并开始生成…' : '正在保存风格…'; notice.dataset.error = 'false';
    try { await apply(choices.find(style => style.id === select.value), { replace: true, generate }); dialog.close(); }
    catch (error) { notice.textContent = error.message; notice.dataset.error = 'true'; }
    finally { working = false; select.disabled = false; dialog.querySelectorAll('button').forEach(node => { node.disabled = false; }); }
  } }, el('label', { class: 'zs-field' }, el('span', {}, '室内风格'), select), note,
    el('p', { class: 'zs-help' }, '按新风格生成一套室内效果，原图和已有结果都会保留。'),
    el('footer', { class: 'zt-footer' }, notice,
      el('button', { type: 'submit', class: 'zs-btn primary' }, '按这个风格生成'),
      el('button', { type: 'submit', class: 'zs-btn quiet', 'data-save-only': '1' }, '只保存风格')));
  dialog.append(el('header', { class: 'zt-head' }, el('h2', {}, '换个风格'),
    el('button', { type: 'button', class: 'zs-btn quiet', onclick: () => { if (!working) dialog.close(); } }, '关闭')), form);
  dialog.addEventListener('cancel', event => { if (working) event.preventDefault(); });
  dialog.addEventListener('close', () => dialog.remove(), { once: true });
  document.body.append(dialog); dialog.showModal(); return dialog;
}

export function closeInteriorPhotos() {
  document.querySelector('.zp-photo-dialog[open]')?.close();
}

export function openInteriorPhotos({ photos, selectedId, opener }) {
  if (!photos.length) return null;
  closeInteriorPhotos();
  let index = Math.max(0, photos.findIndex(photo => photo.id === selectedId));
  const dialog = el('dialog', { class: 'zs-tool-dialog zp-photo-dialog', 'aria-label': '查看室内效果' });
  const title = el('h2');
  const count = el('p', { class: 'zs-help', 'aria-live': 'polite' });
  const notice = el('p', { class: 'zs-help', role: 'status' });
  const photo = el('img', { alt: '', draggable: 'false', onload: () => { notice.textContent = ''; },
    onerror: () => { notice.textContent = '这张图片暂时未能读取，可以打开原图查看。'; } });
  const original = el('a', { class: 'zs-btn quiet small', target: '_blank', rel: 'noopener' }, '打开原尺寸图片');
  const previous = el('button', { type: 'button', class: 'zs-btn quiet', onclick: () => move(-1) }, '上一张');
  const next = el('button', { type: 'button', class: 'zs-btn quiet', onclick: () => move(1) }, '下一张');
  const edit = el('button', { type: 'button', class: 'zs-btn primary', onclick: () => {
    const action = photos[index].edit;
    dialog.close(); action?.();
  } }, '在这张上改');
  function paint() {
    const current = photos[index];
    title.textContent = current.label;
    count.textContent = `${index + 1} / ${photos.length}`;
    notice.textContent = '正在读取图片…';
    photo.alt = current.label; photo.src = current.url; original.href = current.url;
    previous.hidden = next.hidden = photos.length === 1;
    previous.disabled = index === 0; next.disabled = index === photos.length - 1;
    edit.hidden = !current.edit;
  }
  function move(direction) {
    const candidate = index + direction;
    if (candidate < 0 || candidate >= photos.length) return;
    index = candidate; paint();
  }
  dialog.addEventListener('keydown', event => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault(); move(event.key === 'ArrowLeft' ? -1 : 1);
    }
  });
  const body = el('div', { class: 'zp-photo-body', style: 'touch-action:pan-y pinch-zoom' }, photo, notice);
  let gesture = null;
  body.addEventListener('pointerdown', event => {
    if (event.pointerType === 'mouse') return;
    if (event.isPrimary === false) { gesture = null; return; }
    gesture = { id: event.pointerId, x: event.clientX, y: event.clientY };
    try { body.setPointerCapture?.(event.pointerId); } catch {}
  });
  body.addEventListener('pointerup', event => {
    if (!gesture || gesture.id !== event.pointerId) return;
    const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
    gesture = null;
    if (Math.abs(dx) >= 48 && Math.abs(dx) > Math.abs(dy) * 1.5) move(dx < 0 ? 1 : -1);
  });
  body.addEventListener('pointercancel', () => { gesture = null; });
  body.addEventListener('lostpointercapture', () => { gesture = null; });
  dialog.append(el('header', { class: 'zt-head' }, el('div', {}, title, count),
    el('button', { type: 'button', class: 'zs-btn quiet', onclick: () => dialog.close() }, '关闭')),
    body,
    el('footer', { class: 'zt-footer' }, el('div', { class: 'zs-row' }, previous, next), edit, original));
  dialog.addEventListener('close', () => {
    dialog.remove();
    if (opener?.isConnected) opener.focus();
    else document.querySelector('.zs-studio[open] main')?.focus({ preventScroll: true });
  }, { once: true });
  document.body.append(dialog); paint(); dialog.showModal(); return dialog;
}
