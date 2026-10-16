import { toolDraft } from './studio-tool-drafts.js?v=19.7';

const ns = 'http://www.w3.org/2000/svg';
const sourceLabels = ['实测', '图纸', '产品资料', '设计目标'];
const unitScales = { mm: 1, cm: 10, m: 1000 };
let opening = false;
function node(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) element.addEventListener(key.slice(2), value);
    else if (key === 'class') element.className = value;
    else if (['value', 'disabled', 'required', 'hidden'].includes(key)) element[key] = value;
    else element.setAttribute(key, value);
  }
  element.append(...children.flat().filter(v => v != null)); return element;
}
function svg(tag, attrs = {}, text = '') {
  const element = document.createElementNS(ns, tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  element.textContent = text; return element;
}
export function dimensionLabel(row) { return `${row.name || '部位'}  ${row.value || '—'} ${row.unit}`; }
export function dimensionPreviewLabel(row, index, availableWidth, measure) {
  const name = Array.from(row.name || '部位');
  const prefix = `${index + 1}. `;
  const full = prefix + dimensionLabel(row);
  if (measure(full) <= availableWidth) return full;
  while (name.length) {
    name.pop();
    const label = prefix + dimensionLabel({ ...row, name: name.join('') + '…' });
    if (measure(label) <= availableWidth) return label;
  }
  const value = `${prefix}${row.value || '—'} ${row.unit}`;
  return measure(value) <= availableWidth ? value : `${index + 1}.`;
}
export function dimensionPreviewPosition(preferred, bounds, occupied = [], gap = 6) {
  const fit = (x, y) => ({ ...preferred,
    x: Math.min(Math.max(bounds.x, bounds.x + bounds.width - preferred.width), Math.max(bounds.x, x)),
    y: Math.min(Math.max(bounds.y, bounds.y + bounds.height - preferred.height), Math.max(bounds.y, y)) });
  const overlaps = rect => occupied.some(other => other && rect.x < other.x + other.width + gap
    && rect.x + rect.width + gap > other.x && rect.y < other.y + other.height + gap
    && rect.y + rect.height + gap > other.y);
  const original = fit(preferred.x, preferred.y);
  if (!overlaps(original)) return original;
  const adjacent = occupied.filter(Boolean).flatMap(other => [
    fit(original.x, other.y - preferred.height - gap),
    fit(original.x, other.y + other.height + gap),
    fit(other.x - preferred.width - gap, original.y),
    fit(other.x + other.width + gap, original.y),
  ]).sort((a, b) => Math.hypot(a.x - original.x, a.y - original.y) - Math.hypot(b.x - original.x, b.y - original.y));
  const nearest = adjacent.find(candidate => !overlaps(candidate));
  if (nearest) return nearest;
  for (let distance = 1; distance <= occupied.length + 1; distance++) {
    for (const dx of [0, -preferred.width - gap, preferred.width + gap]) {
      for (const direction of [-1, 1]) {
        const candidate = fit(original.x + dx, original.y + direction * distance * (preferred.height + gap));
        if (!overlaps(candidate)) return candidate;
      }
    }
  }
  return original;
}
export function clampPoint(x, y) { return [Math.min(1, Math.max(0, x)), Math.min(1, Math.max(0, y))]; }
export function dimensionRecordPage(records, count = 10) { return records.slice().reverse().slice(0, count); }
export function dimensionItemsMatch(saved, expected) {
  const normalize = rows => rows.map(row => ({ id: row.id, name: row.name.trim(), value: row.value, unit: row.unit,
    source: row.source, note: row.note.trim(), points: row.points.map(point => point.map(value => Number(value.toFixed(6)))) }));
  try { return JSON.stringify(normalize(saved)) === JSON.stringify(normalize(expected)); }
  catch { return false; }
}
export function annotationIssue(items) {
  for (const [index, item] of items.entries()) {
    const error = (field, message) => ({ index, field, message: `第 ${index + 1} 条：${message}` });
    if (!String(item.name || '').trim()) return error('name', '请填写物件或部位名称。');
    if (!Number.isFinite(item.value) || item.value <= 0 || !unitScales[item.unit]
      || item.value * unitScales[item.unit] > 1000000) return error('value', '尺寸须大于 0，换算后不能超过 1000 米。');
    if (!sourceLabels.includes(item.source)) return error('source', '请选择尺寸来源。');
    if (!String(item.note || '').trim()) return error('note', '请填写数值的依据。');
    if (!Array.isArray(item.points) || item.points.length !== 2 || item.points.some(p => !Array.isArray(p) || p.length !== 2
      || p.some(v => !Number.isFinite(v) || v < 0 || v > 1))
      || Math.hypot(item.points[0][0] - item.points[1][0], item.points[0][1] - item.points[1][1]) < .003)
      return error('points', '请设置两个不同的端点。');
  }
  return null;
}

export function dimensionDraftValid(value) {
  const point = p => Array.isArray(p) && p.length === 2 && p.every(n => Number.isFinite(n) && n >= 0 && n <= 1);
  if (!value || typeof value.baseline !== 'string' || value.baseline.length > 64000 || typeof value.dirty !== 'boolean'
    || !Array.isArray(value.items) || value.items.length > 24) return false;
  const ids = new Set();
  for (const row of value.items) {
    if (!row || typeof row.id !== 'string' || !/^[\w-]{1,80}$/.test(row.id) || ids.has(row.id)
      || typeof row.name !== 'string' || row.name.length > 60 || typeof row.note !== 'string' || row.note.length > 200
      || row.value !== '' && row.value !== null && !Number.isFinite(row.value) || !unitScales[row.unit]
      || !['', ...sourceLabels].includes(row.source) || !Array.isArray(row.points) || row.points.length !== 2 || !row.points.every(point)) return false;
    ids.add(row.id);
  }
  if (value.selected != null && !ids.has(value.selected)) return false;
  const p = value.placement;
  return p === null || Boolean(p && ids.has(p.id) && [0, 1].includes(p.count)
    && Array.isArray(p.points) && p.points.length === p.count && p.points.every(point));
}

export async function openDimensions({ owner = '', image, url, load, save, exportSheet, isCurrent = () => true }) {
  const existing = document.querySelector('.zd-dialog[open]');
  if (opening || existing) { existing?.focus(); return existing; }
  opening = true;
  let initial;
  try { initial = await load(); if (!isCurrent()) return; }
  finally { opening = false; }
  const items = structuredClone(initial.document?.items || []);
  let selected = items[0]?.id, placement = null, drag = null, working = false, dirty = false, hasSavedDocument = Boolean(initial.document);
  let baseline = JSON.stringify(initial.document || null), storageError = '', saveBlocked = false, exportBlocked = false, refreshBlocked = false;
  const drafts = toolDraft('dimensions', [owner, image.id, initial.source_sha256], dimensionDraftValid);
  const recovered = drafts.read();
  let recoveryPending = Boolean(recovered.value) || !recovered.ok;
  const notice = node('p', { class: 'zd-notice', role: 'status', 'aria-live': 'polite' });
  const exportLinks = node('div', { class: 'zs-row zd-export-links', hidden: true, 'aria-label': '刚才导出的固定文件' });
  const list = node('div', { class: 'zd-list' }), fields = node('div', { class: 'zd-fields' });
  const recordNotice = node('p', { class: 'zs-help', role: 'status', hidden: true });
  const recordsArea = node('div', { class: 'zd-records', 'aria-label': '已保存的尺寸记录' });
  const viewWidth = 1000, viewHeight = 1000 * image.height / image.width;
  const overlay = svg('svg', { viewBox: `0 0 ${viewWidth} ${viewHeight}`, class: 'zd-overlay', 'aria-label': '尺寸标注预览' });
  const area = node('div', { class: 'zd-image', style: `aspect-ratio:${image.width}/${image.height}` },
    node('img', { src: url, alt: '标注底图，原图不会改动', draggable: 'false' }), overlay);
  const dialog = node('dialog', { class: 'zs-tool-dialog zd-dialog', 'aria-label': '已知尺寸标注' });
  const inform = (text, error = false) => {
    notice.textContent = text + (storageError ? ' ' + storageError : '');
    notice.dataset.error = String(error || Boolean(storageError));
  };
  function persistDraft() {
    if (recoveryPending) return;
    const result = dirty || placement ? drafts.write({ baseline, dirty, items, selected, placement }) : drafts.clear();
    storageError = result.ok ? '' : result.error + ' 请先保存标注，勿刷新或关闭标签页。';
    return result;
  }
  const markDirty = () => { dirty = true; persistDraft(); inform('有未保存的标注修改。'); };
  function recoveryLock() {
    dialog.querySelectorAll('.zd-body input,.zd-body select,.zd-body button,.zt-footer button').forEach(input => { input.disabled = recoveryPending; });
  }
  const recovery = node('section', { class: 'zt-draft-recovery', role: 'region', 'aria-label': '尺寸草稿恢复', hidden: !recoveryPending });
  if (recoveryPending) {
    const changed = recovered.value?.baseline !== baseline;
    recovery.append(node('p', { class: 'zs-help' }, !recovered.ok ? recovered.error + ' 可以清除它后重新填写。'
      : changed ? '找到这个底图的未保存草稿，但服务器上的标注已经变化。恢复前请核对；不会自动替换已保存记录。'
      : '找到这个图片版本的未保存草稿。恢复后仍需保存标注。'),
      ...(recovered.ok ? [node('button', { type: 'button', class: 'zs-btn primary', onclick: () => {
        if (!isCurrent()) { inform('设计已切换，请重新打开标注。', true); return; }
        if (changed && !window.confirm('已保存的标注已经变化。恢复这份未保存草稿作为编辑内容？只有再次点击保存才会替换当前标注。')) return;
        items.splice(0, items.length, ...structuredClone(recovered.value.items));
        selected = recovered.value.selected || items[0]?.id; placement = structuredClone(recovered.value.placement);
        dirty = recovered.value.dirty || changed; recoveryPending = false; recovery.hidden = true;
        paint(); editor(); recoveryLock(); persistDraft();
        inform(placement ? '草稿已恢复。请完成两个端点，或取消重新放置；尚未保存。' : '草稿已恢复，尚未保存到设计。');
      } }, '恢复尺寸草稿')] : []),
      node('button', { type: 'button', class: 'zs-btn quiet', onclick: () => {
        const result = drafts.clear(); storageError = result.ok ? '' : result.error + ' 旧草稿可能仍会出现。';
        recoveryPending = false; recovery.hidden = true; recoveryLock();
        inform(result.ok ? '已放弃本标签页草稿，已保存的标注未改动。' : '本次不恢复草稿，但浏览器未能清除它。', !result.ok);
      } }, '放弃尺寸草稿'));
  }
  const disabledBefore = new Map();
  function lock(value) {
    working = value; dialog.setAttribute('aria-busy', String(value));
    dialog.querySelectorAll('input,select,button').forEach(input => {
      if (value) { disabledBefore.set(input, input.disabled); input.disabled = true; }
      else input.disabled = disabledBefore.get(input) || false;
    });
    if (!value) disabledBefore.clear();
  }
  const act = (label, run, className = 'zs-btn', options = {}) => node('button', { type: 'button', class: className, onclick: async () => {
    if (working || drag || recoveryPending) return;
    try {
      if (options.validate && !validate()) return;
      if (options.lock) lock(true);
      await run();
    } catch (error) { inform(error.message, true); }
    finally { if (options.lock && working) lock(false); }
  } }, label);
  const recordTime = value => {
    const date = new Date(value);
    return value && !Number.isNaN(date.getTime()) ? date.toLocaleString('zh-CN', { hour12: false }) : '保存时间未记录';
  };
  function recordPanel(label, records, renderRecord) {
    if (!records.length) return null;
    const contents = node('div');
    let count = 10;
    const more = act('查看更早的记录', () => { count += 10; fill(); }, 'zs-btn quiet small');
    const fill = () => {
      contents.replaceChildren(...dimensionRecordPage(records, count).map(renderRecord));
      more.hidden = count >= records.length;
    };
    fill();
    return node('details', { class: 'zs-details zd-record-section' }, node('summary', {}, `${label}（${records.length}）`), contents, more);
  }
  function renderRecords(snapshot) {
    recordsArea.replaceChildren(recordNotice,
      ...[recordPanel('历史标注', snapshot.history || [], record => {
        const document = record.document;
        return node('article', { class: 'zd-record-card' }, node('strong', {}, recordTime(document.saved)),
          node('p', { class: 'zs-help' }, '只读记录，不会替换当前标注。'),
          ...(document.source_sha256 !== initial.source_sha256 ? [node('p', { class: 'zs-help' }, '这份记录对应的底图与当前底图不同，请核对原图。')] : []),
          ...(document.items?.length ? document.items.map(item => node('div', { class: 'zd-record-value' },
            node('strong', {}, dimensionLabel(item)), node('span', {}, item.source || '来源类型未记录'),
            node('p', {}, item.note || '依据未记录'))) : [node('p', { class: 'zs-help' }, '这份记录没有尺寸标注。')]),
          node('a', { class: 'zs-link', href: record.url, download: '' }, '下载历史标注 JSON'));
      }), recordPanel('已导出的尺寸图', snapshot.exports || [], record => node('article', { class: 'zd-record-card' },
        node('strong', {}, recordTime(record.saved)), node('p', { class: 'zs-help' }, '下载这次导出时的文件，不按当前标注重做。'),
        record.url ? node('div', { class: 'zs-row' }, node('a', { class: 'zs-link', href: record.url, download: '' }, '下载尺寸图 PNG'),
          node('a', { class: 'zs-link', href: record.metadata_url, download: '' }, '下载尺寸记录 JSON'))
          : node('p', { class: 'zs-help' }, '这份导出记录缺少下载标识。')))].filter(Boolean));
  }
  async function refreshRecords() {
    try {
      const snapshot = await load();
      if (!isCurrent()) return false;
      recordNotice.hidden = true; renderRecords(snapshot);
      return true;
    } catch {
      recordNotice.hidden = false; recordNotice.textContent = '记录列表暂未刷新，请稍后重新打开标注。';
      return false;
    }
  }
  renderRecords(initial);
  const current = () => items.find(item => item.id === selected);
  function paint() {
    if (drag) return;
    area.dataset.placing = String(Boolean(placement));
    overlay.replaceChildren();
    const scale = viewWidth / (area.clientWidth || 700), fontSize = 13 * scale;
    const labelPositions = [];
    items.forEach((item, index) => {
      const color = item.id === selected ? '#cf9a44' : '#568784';
      const lines = ['#fff', color].map((stroke, i) => svg('line', { stroke, 'stroke-width': i ? 2 : 6,
        'vector-effect': 'non-scaling-stroke', 'data-dimension-line': item.id }));
      const plate = svg('rect', { rx: 4 * scale, fill: '#182b2b', opacity: '.92', 'pointer-events': 'none', 'data-dimension-label': item.id });
      const text = svg('text', { fill: '#fff', 'font-size': fontSize, 'font-family': 'sans-serif', 'pointer-events': 'none' },
        `${index + 1}. ${dimensionLabel(item)}`);
      text.append(svg('title', {}, dimensionLabel(item)));
      overlay.append(...lines, plate, text);
      const measure = label => { text.firstChild.textContent = label; return text.getComputedTextLength(); };
      text.firstChild.textContent = dimensionPreviewLabel(item, index, viewWidth - 28 * scale, measure);
      const points = placement?.id === item.id && placement.count ? [placement.points[0], placement.points[0]] : item.points;
      const updateLine = () => {
        const [a, b] = points.map(p => [p[0] * viewWidth, p[1] * viewHeight]);
        for (const line of lines) for (const [key, value] of Object.entries({ x1: a[0], y1: a[1], x2: b[0], y2: b[1] })) line.setAttribute(key, value);
        const width = Math.min(viewWidth - 12 * scale, Math.max(100 * scale, text.getComputedTextLength() + 16 * scale));
        const x = Math.min(viewWidth - width - 6 * scale, Math.max(6 * scale, (a[0] + b[0] - width) / 2));
        const y = Math.min(viewHeight - 6 * scale, Math.max(30 * scale, (a[1] + b[1]) / 2 - 12 * scale));
        const label = dimensionPreviewPosition({ x, y: y - 20 * scale, width, height: 27 * scale },
          { x: 6 * scale, y: 6 * scale, width: viewWidth - 12 * scale, height: viewHeight - 12 * scale },
          labelPositions.filter((_, position) => position !== index), 2 * scale);
        labelPositions[index] = label;
        for (const [key, value] of Object.entries(label)) plate.setAttribute(key, value);
        text.setAttribute('x', label.x + 8 * scale); text.setAttribute('y', label.y + 20 * scale);
      };
      updateLine();
      points.forEach((point, endpoint) => {
        if (placement?.id === item.id && placement.count && endpoint === 1) return;
        const handle = svg('g', { transform: `translate(${point[0] * viewWidth} ${point[1] * viewHeight})`,
          tabindex: 0, role: 'button', class: 'zd-handle', 'aria-label': `标注 ${index + 1} 端点 ${endpoint + 1}` });
        handle.append(svg('circle', { r: 22 * scale, fill: 'transparent', 'data-dimension-hit': '1' }),
          svg('circle', { r: 5 * scale, fill: color, stroke: '#fff', 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke' }));
        handle.addEventListener('pointerdown', event => {
          if (working || recoveryPending || placement || drag || event.button !== 0) return;
          event.preventDefault(); event.stopPropagation(); selected = item.id;
          handle.setPointerCapture(event.pointerId);
          const move = e => {
            if (e.pointerId !== event.pointerId || working) return;
            const rect = area.getBoundingClientRect();
            item.points[endpoint] = clampPoint((e.clientX - rect.left) / rect.width, (e.clientY - rect.top) / rect.height);
            handle.setAttribute('transform', `translate(${item.points[endpoint][0] * viewWidth} ${item.points[endpoint][1] * viewHeight})`);
            updateLine(); markDirty();
          };
          const finish = e => {
            if (drag?.handle !== handle || e && e.pointerId !== event.pointerId) return;
            drag = null;
            handle.removeEventListener('pointermove', move);
            for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) handle.removeEventListener(type, finish);
            if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
            if (dialog.open) { paint(); editor(); }
          };
          drag = { handle, finish };
          handle.addEventListener('pointermove', move);
          for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) handle.addEventListener(type, finish);
        });
        handle.addEventListener('keydown', event => {
          const steps = { ArrowLeft: [-.005, 0], ArrowRight: [.005, 0], ArrowUp: [0, -.005], ArrowDown: [0, .005] };
          if (!steps[event.key] || working || recoveryPending || placement || drag) return;
          event.preventDefault(); const delta = steps[event.key];
          selected = item.id; placement = null;
          item.points[endpoint] = clampPoint(point[0] + delta[0], point[1] + delta[1]); markDirty(); paint(); editor();
          overlay.querySelector(`[aria-label="标注 ${index + 1} 端点 ${endpoint + 1}"]`)?.focus();
        });
        overlay.append(handle);
      });
    });
    list.replaceChildren(...items.map((item, index) => act(`${String(index + 1).padStart(2, '0')}  ${dimensionLabel(item)}`,
      () => { selected = item.id; placement = null; persistDraft(); editor(); paint(); }, 'zd-item' + (selected === item.id ? ' selected' : ''))));
  }
  function editor() {
    fields.replaceChildren(); const item = current();
    if (!item) { fields.append(node('p', { class: 'zs-help' }, '添加一条已知尺寸。数值和来源由你填写，不会按图片估算。')); return; }
    const control = (label, key, options = {}) => {
      const input = options.options ? node('select', { 'aria-label': label }, options.options.map(value => node('option', { value }, value || '请选择来源类型')))
        : node('input', { type: options.type || 'text', required: true, maxlength: options.maxlength || 200, 'aria-label': label,
          ...(options.type === 'number' ? { min: '.000001', max: 1000000 / unitScales[item.unit], step: 'any' } : {}) });
      input.dataset.dimensionField = key;
      input.value = item[key]; input.addEventListener('input', () => {
        item[key] = options.type === 'number' && input.value !== '' ? Number(input.value) : input.value;
        if (key === 'unit') fields.querySelector('[data-dimension-field=value]').max = 1000000 / unitScales[item.unit];
        const sourceHelp = fields.querySelector('[data-source-help]');
        if (sourceHelp && key === 'source') sourceHelp.textContent = item.source === '设计目标' ? '这是目标尺寸，不是实测结果。' : '写明图纸名称、资料页或测量记录，便于复核。';
        markDirty(); paint();
      });
      return node('label', { class: 'zs-field' }, node('span', {}, label), input);
    };
    fields.append(control('物件或部位名称', 'name', { maxlength: 60 }),
      node('div', { class: 'zd-pair' }, control('已知尺寸', 'value', { type: 'number' }), control('单位', 'unit', { options: ['mm', 'cm', 'm'] })),
      control('尺寸来源', 'source', { options: ['', ...sourceLabels] }), control('依据说明', 'note'),
      node('p', { class: 'zs-help', 'data-source-help': '1' }, item.source === '设计目标' ? '这是目标尺寸，不是实测结果。' : '写明图纸名称、资料页或测量记录，便于复核。'),
      act('在图上点两个端点', () => { placement = { count: 0, id: item.id, points: [] }; persistDraft(); paint(); editor(); area.scrollIntoView({ block: 'center' }); inform('先点起点，再点终点。完成后可拖动端点或用方向键微调。'); }),
      ...(placement ? [act('取消重新放置', () => { placement = null; persistDraft(); paint(); editor(); inform('已取消重新放置，原端点保留。'); }, 'zs-btn quiet')] : []),
      act('删除这一条', () => { items.splice(items.indexOf(item), 1); selected = items[0]?.id; placement = null; markDirty(); paint(); editor(); }, 'zs-btn quiet'));
  }
  area.addEventListener('click', event => {
    if (!placement || working || recoveryPending) return;
    const item = items.find(v => v.id === placement.id), rect = area.getBoundingClientRect();
    const point = clampPoint((event.clientX - rect.left) / rect.width, (event.clientY - rect.top) / rect.height);
    if (placement.count === 1 && Math.hypot(point[0] - placement.points[0][0], point[1] - placement.points[0][1]) < .003) {
      inform('终点与起点太近，请重新点终点。', true); return;
    }
    placement.points[placement.count] = point;
    if (++placement.count === 2) { item.points = placement.points; placement = null; markDirty(); inform('端点已设置。'); editor(); }
    else { persistDraft(); inform('再点终点。'); }
    paint();
  });
  function validate() {
    if (placement) {
      inform(placement.count ? '请先点终点，或取消重新放置。' : '请先设置两个端点，或取消重新放置。', true);
      area.scrollIntoView({ block: 'center' }); return false;
    }
    const issue = annotationIssue(items);
    if (!issue) return true;
    selected = items[issue.index].id; placement = null; paint(); editor(); inform(issue.message, true);
    const control = issue.field === 'points' ? fields.querySelector('button') : fields.querySelector(`[data-dimension-field=${issue.field}]`);
    control?.focus(); control?.scrollIntoView({ block: 'nearest' });
    return false;
  }
  const commit = async () => {
    if (refreshBlocked) throw Error('尺寸图已保存，但设计记录还未刷新。请关闭并重新打开标注，核对后再保存；未保存草稿仍保留。');
    if (saveBlocked) throw Error('已保存的标注可能已变化。请关闭并重新打开，核对最新记录后再保存。未保存草稿仍保留。');
    if (placement || annotationIssue(items)) throw Error('请先完成端点放置并补齐标注内容。');
    let saved;
    try { saved = await save({ image_id: image.id, source_sha256: initial.source_sha256, expected_document: JSON.parse(baseline), items }); }
    catch (error) {
      if (error.submissionUncertain || /已更新|另一窗口|别处改过|页面已更新/.test(error.message)) {
        saveBlocked = true; persistDraft();
        throw Error('标注保存未确认，已保存内容可能已变化。请关闭并重新打开，核对最新记录后再保存。未保存草稿仍保留。');
      }
      throw error;
    }
    if (saved && typeof saved === 'object' && saved.source_sha256 === initial.source_sha256 && Array.isArray(saved.items)) {
      baseline = JSON.stringify(saved);
      dirty = false; hasSavedDocument = true;
      const result = drafts.clear(); storageError = result.ok ? '' : result.error + ' 已保存记录不受影响，旧草稿可能仍会出现。';
      inform(items.length ? '标注已保存到这个图片版本。原图未改动。' : '这个版本的标注已清空。原图未改动。');
      await refreshRecords(); return true;
    }
    throw Error('未能保存，请重新打开这个版本。');
  };
  const exportDocument = async () => {
    if (refreshBlocked) throw Error('尺寸图已保存，但设计记录还未刷新。请关闭并重新打开标注后再导出；刚才的下载链接仍可使用。');
    if (exportBlocked) throw Error('导出结果尚未确认。请关闭并重新打开，核对已导出记录后再操作。');
    if (!items.length) throw Error('请先添加至少一条尺寸标注，再导出尺寸图。');
    if (dirty || !hasSavedDocument) await commit();
    let result;
    try { result = await exportSheet({ expected_source_sha256: initial.source_sha256, expected_items: items }); }
    catch (error) { if (error.submissionUncertain) exportBlocked = true; throw error; }
    const refreshed = await refreshRecords();
    refreshBlocked = Boolean(result.refresh_failed && !refreshed);
    const link = node('a', { href: result.url, download: '', class: 'zs-link' }, '下载尺寸图');
    inform((result.recovered ? '已找回对应的固定尺寸图，未重复导出。' : '尺寸图已保存。')
      + (refreshBlocked ? '设计记录暂未刷新，请关闭并重新打开标注后再保存或导出。' : ''));
    exportLinks.hidden = false;
    exportLinks.replaceChildren(node('small', {}, '刚才导出的固定文件：'), link,
      ...(result.metadata_url ? [' ', node('a', { href: result.metadata_url, download: '', class: 'zs-link' }, '下载尺寸记录 JSON')] : []),
      ...(result.path ? [node('details', { class: 'zd-export-location' }, node('summary', {}, '查看 Mac 保存位置'), node('code', {}, result.path))] : []));
  };
  const requestClose = () => {
    if (working) return;
    if (!recoveryPending && (dirty || placement)) {
      persistDraft();
      if (!window.confirm(storageError ? '浏览器无法暂存这次修改。关闭会丢失未保存内容，仍然关闭？'
        : '标注尚未保存。关闭后保留本标签页草稿，下次打开可恢复；关闭标签页后不保证保留。现在关闭？')) return;
    }
    dialog.close();
  };
  dialog.append(node('header', { class: 'zt-head' }, node('div', {}, node('small', {}, '版本 ' + image.version + '  /  原图独立保存'),
    node('h2', {}, '已知尺寸标注')), node('button', { type: 'button', class: 'zs-btn quiet', onclick: requestClose }, '关闭')), recovery,
    node('div', { class: 'zd-body' }, node('section', { class: 'zd-preview' }, area,
      node('p', { class: 'zs-help' }, '标注线表示部位。透视效果图不能作为测量工具，也不能替代加工图纸。'),
      node('p', { class: 'zs-help' }, '未保存内容仅暂存在当前浏览器标签页，不是设计记录；关闭标签页后不保证保留。')),
    node('aside', { class: 'zd-sidebar' }, act('＋ 添加尺寸', () => {
      if (items.length >= 24) throw Error('最多24条标注。');
      const item = { id: crypto.randomUUID().replaceAll('-', ''), name: '', value: 0, unit: 'mm', source: '', note: '',
        points: [[.2, .25 + items.length % 6 * .08], [.8, .25 + items.length % 6 * .08]] };
      items.push(item); selected = item.id; placement = null; markDirty(); paint(); editor(); fields.querySelector('input')?.focus();
    }, 'zs-btn primary'), initial.known_values?.length ? node('details', { class: 'zs-details' },
      node('summary', {}, '从已记录尺寸或当前目标带入'), node('p', { class: 'zs-help' }, '先核对数值与来源，再选择端点。历史记录未注明来源类型时需手动选择；当前目标可能晚于这张图。'),
      initial.known_values.map(value => act(dimensionLabel(value) + ' · ' + (value.source || '来源待确认'), () => {
        if (items.length >= 24) throw Error('最多24条标注。');
        const vertical = /高|长/.test(value.name), index = items.length;
        const item = { ...value, id: crypto.randomUUID().replaceAll('-', ''),
          points: vertical ? [[.75 + index % 3 * .06, .15], [.75 + index % 3 * .06, .82]] : [[.18, .82 - index % 3 * .06], [.82, .82 - index % 3 * .06]] };
        items.push(item); selected = item.id; placement = null; markDirty(); paint(); editor();
      }, 'zs-btn quiet small'))) : null, list, fields, recordsArea)),
    node('footer', { class: 'zt-footer' }, node('div', { class: 'zd-status' }, notice, exportLinks), node('div', { class: 'zs-row' },
      act('保存标注', commit, 'zs-btn primary', { validate: true, lock: true }),
      act('导出尺寸图', exportDocument, 'zs-btn', { validate: true, lock: true }))));
  dialog.addEventListener('close', () => { drag?.finish(); dialog.remove(); }, { once: true });
  dialog.addEventListener('cancel', event => { event.preventDefault(); requestClose(); });
  const resize = new ResizeObserver(() => { if (!working) paint(); });
  dialog.addEventListener('close', () => resize.disconnect(), { once: true });
  const beforeUnload = event => {
    if (recoveryPending || !dirty && !placement) return;
    persistDraft(); event.preventDefault(); event.returnValue = '';
  };
  const pageHide = () => { if (!recoveryPending && (dirty || placement)) persistDraft(); };
  window.addEventListener('beforeunload', beforeUnload); window.addEventListener('pagehide', pageHide);
  dialog.addEventListener('close', () => {
    window.removeEventListener('beforeunload', beforeUnload); window.removeEventListener('pagehide', pageHide);
  }, { once: true });
  document.body.append(dialog); dialog.showModal(); paint(); editor(); recoveryLock(); resize.observe(area); return dialog;
}
