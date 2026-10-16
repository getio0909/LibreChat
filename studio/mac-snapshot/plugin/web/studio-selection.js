function node(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) element.addEventListener(key.slice(2), value);
    else element.setAttribute(key, value);
  }
  for (const child of children) element.append(child);
  return element;
}

export function paintSelection(canvas, selection, color = '#007aff') {
  const g = canvas.getContext('2d');
  g.clearRect(0, 0, canvas.width, canvas.height);
  for (const shape of selection?.shapes || []) {
    const points = shape.points.map(([x, y]) => [x / 999 * (canvas.width - 1), y / 999 * (canvas.height - 1)]);
    g.globalCompositeOperation = shape.op === 'subtract' ? 'destination-out' : 'source-over';
    g.fillStyle = g.strokeStyle = color;
    g.beginPath();
    if (shape.type === 'rect') {
      const [a, b] = points;
      g.fillRect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(a[0] - b[0]) + 1, Math.abs(a[1] - b[1]) + 1);
    } else if (shape.type === 'stroke') {
      const radius = shape.radius / 999 * Math.min(canvas.width, canvas.height);
      g.lineWidth = radius * 2; g.lineCap = g.lineJoin = 'round';
      points.forEach(([x, y], i) => i ? g.lineTo(x, y) : g.moveTo(x, y));
      g.stroke();
      for (const [x, y] of points) { g.beginPath(); g.arc(x, y, radius, 0, Math.PI * 2); g.fill(); }
    } else {
      points.forEach(([x, y], i) => i ? g.lineTo(x, y) : g.moveTo(x, y));
      g.closePath(); g.fill('evenodd');
    }
  }
  g.globalCompositeOperation = 'source-over';
}

export function selectionOverlay(selection, width, height) {
  const scale = Math.min(1, 1600 / Math.max(width, height));
  const canvas = node('canvas', { class: 'zs-selection-overlay', width: Math.round(width * scale), height: Math.round(height * scale),
    'aria-label': '本次修改范围' });
  paintSelection(canvas, selection);
  return canvas;
}

export function openSelection({ url, initial, title, reference, owner, api, segmentReady, commit, intent = 'source' }) {
  const copy = {
    source: { hint: '选出想从参考照片中取用的部分。', footer: '只取用蓝色部分，其余不作为参考。', confirm: '确认取用部分' },
    edit: { hint: '选出这张图上要修改的地方，其余保留原图。', footer: '只修改蓝色范围，其余保留原图。', confirm: '确认修改范围' },
    target: { hint: '在底图上框出物件的放置位置。', footer: '这是物件的位置，不会替代整体修改范围。', confirm: '确认放置位置' },
  }[intent] || { hint: '选出想取用的部分。', footer: '只取用蓝色部分。', confirm: '确认取用部分' };
  let selection = structuredClone(initial || { shapes: [] }), history = [], active = null, closed = false, pending = false;
  const original = JSON.stringify(selection);
  let imageReady = false, hasPixels = false;
  let tool = intent === 'target' ? 'rect' : segmentReady ? 'point' : 'rect', operation = 'add', points = [], pointLabels = [];
  const dialog = node('dialog', { class: 'zs-selection-dialog', 'aria-label': title });
  const makeButton = (label, run) => node('button', { type: 'button', class: 'zs-btn quiet small', onclick: run }, label);
  const message = node('p', { class: 'zs-selection-message', role: 'status' }, copy.hint);
  const img = node('img', { src: url, alt: title, draggable: 'false' });
  const canvas = node('canvas', { class: 'zs-selection-overlay', 'aria-label': title + '画布', tabindex: '0' });
  const frame = node('div', { class: 'zs-selection-frame' }, img, canvas);
  const scroller = node('div', { class: 'zs-selection-scroll' }, frame);
  const tools = node('div', { class: 'zs-selection-tools', role: 'group', 'aria-label': '选择工具' });
  const buttons = new Map();
  const refresh = () => {
    paintSelection(canvas, selection);
    if (!active) {
      hasPixels = false;
      const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      for (let i = 3; i < data.length; i += 4) if (data[i]) { hasPixels = true; break; }
    }
    for (const [key, button] of buttons) { button.setAttribute('aria-pressed', String(key === tool)); button.disabled = pending; }
    plus.setAttribute('aria-pressed', String(operation === 'add'));
    minus.setAttribute('aria-pressed', String(operation === 'subtract'));
    plus.disabled = minus.disabled = pending;
    save.disabled = pending || Boolean(active) || !imageReady || !hasPixels;
  };
  const remember = () => { history.push(structuredClone(selection)); if (history.length > 30) history.shift(); };
  for (const [key, label] of [['point', '点选物体'], ['rect', '框选'], ['polygon', '圈选'], ['stroke', '画笔']]) {
    if (key === 'point' && !segmentReady) continue;
    const button = makeButton(label, () => { tool = key; active = null; refresh(); });
    buttons.set(key, button); tools.append(button);
  }
  const plus = makeButton('加选', () => { operation = 'add'; refresh(); });
  const minus = makeButton('减选', () => { operation = 'subtract'; refresh(); });
  tools.append(plus, minus, makeButton('撤销', () => {
    if (!pending && history.length) { selection = history.pop(); points = []; pointLabels = []; message.textContent = '已撤销。' + copy.hint; refresh(); }
  }), makeButton('清空', () => {
    if (!pending) { remember(); selection = { shapes: [] }; points = []; pointLabels = []; message.textContent = '已清空。可以重新选择，或点“撤销”恢复。'; refresh(); }
  }));
  const radius = node('input', { type: 'range', min: '2', max: '70', value: '15', 'aria-label': '画笔大小' });
  tools.append(node('label', { class: 'zs-selection-radius' }, '画笔', radius));
  const fit = () => {
    if (!imageReady || closed) return;
    const width = Math.max(1, Math.min(1000, scroller.clientWidth, scroller.clientHeight * img.naturalWidth / img.naturalHeight));
    frame.dataset.fitWidth = width + 'px';
    frame.style.width = width * (zoom.value === 'fit' ? 1 : Number(zoom.value) / 100) + 'px';
    frame.classList.toggle('zoomed', zoom.value !== 'fit');
  };
  const zoom = node('select', { 'aria-label': '选区放大比例', onchange: fit },
    node('option', { value: 'fit' }, '适合窗口'), node('option', { value: '150' }, '放大 1.5 倍'), node('option', { value: '200' }, '放大 2 倍'));
  tools.append(zoom);
  const sizing = new ResizeObserver(fit);
  const close = () => { closed = true; sizing.disconnect(); dialog.close(); dialog.remove(); };
  const requestClose = () => {
    if (JSON.stringify(selection) !== original && !window.confirm('选区还没确认。关闭并放弃这次修改？')) return;
    close();
  };
  const save = makeButton(copy.confirm, async () => {
    if (pending || active || !imageReady || !hasPixels) return;
    pending = true; refresh(); message.textContent = '正在核对选区…';
    try {
      const proof = await api(`tasks/${owner}/selection`, { image: reference, selection });
      if (closed) return;
      await commit(structuredClone(selection), proof);
      close();
    } catch (error) { if (!closed) message.textContent = error.message; }
    finally { if (!closed) { pending = false; refresh(); } }
  });
  save.classList.add('primary');
  dialog.append(node('div', { class: 'zs-selection-head' }, node('h2', {}, title), makeButton('取消', requestClose)),
    tools, scroller, message, node('div', { class: 'zs-selection-footer' }, node('span', {}, copy.footer), save));
  dialog.addEventListener('cancel', event => { event.preventDefault(); requestClose(); });
  const at = event => {
    const box = canvas.getBoundingClientRect();
    return [Math.max(0, Math.min(999, Math.round((event.clientX - box.left) / box.width * 999))),
      Math.max(0, Math.min(999, Math.round((event.clientY - box.top) / box.height * 999)))];
  };
  canvas.addEventListener('pointerdown', async event => {
    if (pending || event.button !== 0) return;
    event.preventDefault();
    const position = at(event);
    if (tool === 'point') {
      if (operation === 'subtract' && !selection.shapes.length) {
        message.textContent = '先选出物体，再减去不需要的部分。'; return;
      }
      if (points.length >= 24) { message.textContent = '点选次数较多，请用画笔修边或清空后重新选择。'; return; }
      points.push(position); pointLabels.push(operation === 'add' ? 1 : 0);
      pending = true; refresh(); message.textContent = '正在识别所点的物体…';
      try {
        const result = await api(`tasks/${owner}/segment`, { image: reference, points: [position], labels: [1],
          existing: selection.shapes.length ? selection : null, operation });
        if (closed) return;
        remember(); selection = result.selection; points = []; pointLabels = [];
        message.textContent = selection.shapes.length ? '检查蓝色范围和边缘。' + copy.footer : '没有剩余选区。可以重新选择，或点“撤销”恢复。';
      } catch (error) {
        points.pop(); pointLabels.pop(); if (!closed) message.textContent = error.message;
      } finally { if (!closed) { pending = false; refresh(); } }
      return;
    }
    remember(); points = []; pointLabels = [];
    active = { type: tool, op: operation, points: [position], ...(tool === 'stroke' ? { radius: Number(radius.value) } : {}) };
    if (tool === 'rect') active.points.push(position);
    selection.shapes.push(active);
    canvas.setPointerCapture(event.pointerId); refresh();
  });
  canvas.addEventListener('pointermove', event => {
    if (!active || !canvas.hasPointerCapture(event.pointerId)) return;
    const point = at(event), last = active.points.at(-1);
    if (active.type === 'rect') active.points[1] = point;
    else if (Math.hypot(point[0] - last[0], point[1] - last[1]) >= 4 && active.points.length < 1024) active.points.push(point);
    refresh();
  });
  const finish = event => {
    if (!active) return;
    if ((active.type === 'polygon' && active.points.length < 3) || (active.type === 'rect' && Math.hypot(...active.points[0].map((v, i) => v - active.points[1][i])) < 2)) selection = history.pop();
    active = null;
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    refresh();
    if (!hasPixels) selection = { shapes: [] };
    message.textContent = hasPixels ? '检查蓝色范围。' + copy.footer : '没有剩余选区。可以重新选择，或点“撤销”恢复。';
  };
  canvas.addEventListener('pointerup', finish);
  canvas.addEventListener('pointercancel', finish);
  img.addEventListener('load', () => {
    imageReady = true;
    const scale = Math.min(1, 1600 / Math.max(img.naturalWidth, img.naturalHeight));
    canvas.width = Math.round(img.naturalWidth * scale); canvas.height = Math.round(img.naturalHeight * scale);
    frame.style.aspectRatio = `${img.naturalWidth}/${img.naturalHeight}`;
    fit(); refresh();
  });
  img.addEventListener('error', () => { imageReady = false; message.textContent = '图片未载入，请关闭后重试。'; refresh(); });
  document.body.append(dialog); dialog.showModal(); sizing.observe(scroller); refresh();
  return dialog;
}
