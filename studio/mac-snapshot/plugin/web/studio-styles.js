export function referenceUse(role) {
  return { '形态': 'shape', '材质': 'material', '风格': 'style', '文化依据': 'reference' }[role] || 'reference';
}
export function styleReferences(saved, rows, incoming, curated, replace = false, suppressed = new Set()) {
  const selected = new Map(saved.map(ref => [ref.id, { ...ref }]));
  const uses = { style: '风格', material: '材质', shape: '形态', object: '形态', pattern: '形态', reference: '形态' };
  const current = new Map(rows.filter(row => row.source === 'reference' && curated.has(row.id)).map(row => [row.id, row]));
  const incomingIds = new Set(incoming.map(ref => ref.id));
  if (replace) for (const [id, ref] of selected) {
    const row = current.get(id);
    if (curated.has(id) && (row ? row.use === 'style' : ref.role === '风格')) selected.delete(id);
  }
  for (const [id, row] of current) if (!(replace && row.use === 'style' && !incomingIds.has(id)) && uses[row.use])
    selected.set(id, { id, role: row.use === 'reference' && selected.get(id)?.role === '文化依据' ? '文化依据' : uses[row.use],
      use: row.use, take: row.take || '',
      ...(row.source_selection ? { source_selection: row.source_selection, source_sha256: row.source_sha256 } : {}) });
  for (const ref of incoming) {
    if (selected.has(ref.id) && !(suppressed.has(ref.id) && !current.has(ref.id))) continue;
    const row = current.get(ref.id);
    selected.set(ref.id, { id: ref.id, role: '风格', use: 'style',
      take: ref.style_take || '只参考材质搭配、装饰层次和光线，不照搬人物或宗教细节。',
      ...(row?.source_selection && !(replace && row.use === 'style')
        ? { source_selection: row.source_selection, source_sha256: row.source_sha256 } : {}) });
  }
  return [...selected.values()];
}
export function styleEditRows(rows, incoming, curated, selected, replace = false, referenceBase = '/zyb/studio/reference-image/') {
  const incomingIds = new Set(incoming.map(ref => ref.id));
  const result = rows.filter(row => !(replace && row.source === 'reference' && row.use === 'style'
    && curated.has(row.id) && !incomingIds.has(row.id))).map(row => ({ ...row }));
  for (const ref of incoming) {
    if (result.some(row => row.source === 'reference' && row.id === ref.id)) continue;
    const saved = selected.find(row => row.id === ref.id);
    const use = saved?.use || referenceUse(saved?.role || '风格');
    result.push({ source: 'reference', id: ref.id, name: ref.title, preview: referenceBase + ref.id,
      target: '', use, fidelity: 'reference', auto: false,
      take: saved?.take ?? (use === 'style' ? (ref.style_take || '只参考材质搭配、装饰层次和光线，不照搬人物或宗教细节。') : ''),
      ...(saved?.source_selection ? { source_selection: saved.source_selection, source_sha256: saved.source_sha256 } : {}) });
  }
  return result;
}
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'class') node.className = value;
    else if (key === 'disabled') node.disabled = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.flat().filter(v => v != null)); return node;
}
export function openStyles({ packs, onUse, disabled = false, selectedIds = [], interior = false }) {
  const notice = el('p', { role: 'status', class: 'zd-notice' });
  const dialog = el('dialog', { class: 'zs-tool-dialog zt-style-dialog', 'aria-label': '精选风格素材' });
  let applying = false;
  const selected = new Set(selectedIds), curated = new Set(packs.flatMap(pack => pack.references.map(ref => ref.id)));
  const hasSelected = selectedIds.some(id => curated.has(id));
  let replace = interior || hasSelected;
  const mode = el('fieldset', { class: 'zt-style-mode' }, el('legend', {}, '选用方式'),
    [['replace', '替换已选精选风格'], ['append', '追加到当前参考']].map(([value, label]) => {
      const input = el('input', { type: 'radio', name: 'style_mode', value,
        onchange: () => { replace = value === 'replace'; } });
      input.checked = replace === (value === 'replace');
      return el('label', { class: 'zs-check' }, input, el('span', {}, label));
    }), el('p', { class: 'zs-help' }, '替换只移除新方向中没有的精选风格。同一素材的取用说明和选区保留，结构底稿和其他手动参考不变。'
      + (interior ? '室内设计替换时会一并更新主风格；追加只增加参考，主风格不变。' : '')));
  const cards = packs.map(pack => el('article', { class: 'zt-style-card' },
    pack.references.length ? el('img', { src: '/zyb/studio/reference-image/' + pack.references[0].id, alt: pack.name, loading: 'lazy' }) : el('div', { class: 'zt-style-missing' }, '素材未就绪'),
    el('div', { class: 'zt-style-copy' }, el('div', { class: 'zt-style-kicker' }, el('span', {}, pack.kind),
      el('div', { class: 'zt-swatches', 'aria-label': '色彩参考' }, pack.colors.map(color => el('span', { style: 'background:' + color, title: color })))),
      el('h3', {}, pack.name), el('small', {}, pack.subtitle), el('p', {}, pack.description),
      el('p', { class: 'zt-style-selection' }, pack.references.every(ref => selected.has(ref.id)) ? '本次已选 · ' + pack.references.length + ' 张'
        : '选用 ' + pack.references.length + ' 张 · 用于材质与氛围'),
      el('p', { class: 'zt-style-caution' }, pack.caution),
      el('details', { class: 'zt-style-details' }, el('summary', {}, '来源、许可与使用范围'),
        el('p', {}, pack.caution), pack.references.map(ref => el('div', { class: 'zt-source' },
          el('strong', {}, ref.title), el('small', {}, ref.credit + ' · ' + ref.rights),
          ref.style_take ? el('p', {}, '取用方向：' + ref.style_take) : null,
          ref.source_url ? el('a', { href: ref.source_url, target: '_blank', rel: 'noopener noreferrer' }, '原始资料页') : el('span', {}, 'AI原创概念，不是真实项目'),
          ref.license_url ? el('a', { href: ref.license_url, target: '_blank', rel: 'noopener noreferrer' }, '许可条款') : null,
          el('a', { href: '/zyb/studio/reference-source/' + ref.id, target: '_blank', rel: 'noopener' }, '查看完整素材')))),
      el('button', { class: 'zs-btn primary', type: 'button', disabled: disabled || !pack.available,
        onclick: async event => {
          if (applying) return;
          const control = event.currentTarget;
          applying = true; control.disabled = true;
          dialog.querySelectorAll('button,input').forEach(input => { input.disabled = true; });
          notice.dataset.error = 'false'; notice.textContent = '正在选用' + pack.name + '…';
          try { await onUse(pack, { replace }); dialog.close(); }
          catch (error) { notice.textContent = error.message; notice.dataset.error = 'true'; }
          finally {
            applying = false;
            dialog.querySelectorAll('input,header button').forEach(input => { input.disabled = false; });
            dialog.querySelectorAll('.zt-style-card button').forEach((button, index) => { button.disabled = disabled || !packs[index].available; });
            control.disabled = disabled || !pack.available;
          }
        } }, pack.available ? '选用这个方向' : '素材校验未通过，暂不可选'))));
  dialog.append(el('header', { class: 'zt-head' }, el('div', {}, el('small', {}, '真实参考与原创概念分别标识'), el('h2', {}, '精选风格素材')),
    el('button', { class: 'zs-btn quiet', type: 'button', onclick: () => { if (!applying) dialog.close(); } }, '关闭')),
    mode, el('div', { class: 'zt-styles' }, cards), el('footer', { class: 'zt-footer' }, notice,
      el('p', { class: 'zs-help' }, '选用后可调整参考用途。只有确认生成时，才发送所选素材。')));
  dialog.addEventListener('cancel', event => { if (applying) event.preventDefault(); });
  dialog.addEventListener('close', () => dialog.remove(), { once: true });
  document.body.append(dialog); dialog.showModal(); return dialog;
}
