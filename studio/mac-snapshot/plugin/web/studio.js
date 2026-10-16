import { openSelection, selectionOverlay } from './studio-selection.js';
import { openDimensions, dimensionItemsMatch } from './studio-dimensions.js?v=20.10';
import { openStyles, referenceUse, styleReferences, styleEditRows } from './studio-styles.js?v=20.18';
import { openFloorplan, floorplanReceipt, floorplanReceiptValid, floorplanMatchesReceipt, floorplanSpecMatch, floorplanViewIssue } from './studio-floorplan.js?v=20.4';
import { openInterior, openInteriorStyle, openInteriorPhotos, closeInteriorPhotos, interiorIssue, interiorBatchProgress, interiorUnresolvedStates, interiorSupplementReference, setInteriorReferenceUse, interiorPreparationIdentity, interiorSpatialOverview } from './studio-interior.js?v=20.47';

const base = '/zyb/studio/';
let panel, view, task, tasks = [], stage = 0, status, refs = [], picked = new Map(), pollTimer;
let renderedStage, renderedTask, planning = false, generationPending = false, navigation = 0, listRequest = 0;
let refsSearched = false, refsLoading = false, refsError = '', refsQuery = '', refsSelectedOnly = false, refsRequest = 0;
let closeMoreMenu = null, actionHintId = 0;
let uploading = false, projectQuery = '', libraryItems = [], libraryQuery = '', draftKey, noticeTimer, activeImageId = null, comparing = false;
let routePush = false, routeLoadEpoch = null, libraryTab = 'archive', draftTimer, renaming = null;
let materials = [], galleryOpen = false;
let stylePacks = [], stylesError = '';
let changesOpen = false;
let compositionTask = null, selectionObserver = null;
let studioBoot = 0, materialsLoading = false, materialsError = '';
const pendingSubmissions = new Map();
const pendingChecks = new Set();
const pendingStops = new Set();
const savingInteriorSets = new Set();
const editDrafts = new Map();
const referenceBudgets = new Map();
const maxEditRows = 32;
const routeTabs = ['brief', 'refs', 'concepts', 'images', 'archive'];
const draftText = new Set(['goal', 'title', 'change', 'comment', 'materials', 'setting', 'audience', 'dimensions', 'cost', 'process',
  'must_have', 'must_avoid', 'culture', 'picked', 'photo', 'variant_target', 'reviewer', 'vision_standard']);
const pendingUploads = new Map();
const imageSelection = new Map();
const drafts = new Map();
const seenImages = new Map();
const refInfo = new Map();
const serverOwnedInputs = new Set(['image_id', 'cloud_consent', 'cloud_consent_provider', 'vision_consent', 'plan_consent']);
const busy = () => planning || uploading || generationPending || pendingSubmissions.size > 0 || task?.job?.status === 'running';
const cloudImages = () => status?.image_provider === 'ark';
const kindLabel = { generate: '效果图', design: '参考方案', refine: '修改版', compose: '组合方案', export: '高清版', upload: '原图' };
const labels = { title: '名称', category: '类别', goal: '想做什么', audience: '给谁用', setting: '摆放位置',
  materials: '材质', dimensions: '尺寸', cost: '预算', process: '加工方式', must_have: '一定要保留',
  must_avoid: '不要出现', culture: '文化与宗教要求', deliverable: '需要的成果' };
const imageURL = (id, owner = task?.id) => base + 'image/' + owner + '/' + id;
const noteURL = (name) => 'obsidian://open?vault=WIKI&file=' + encodeURIComponent(name);
const dateFormat = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const dateText = value => { const date = new Date(value); return Number.isNaN(date.getTime()) ? '' : dateFormat.format(date); };
const icons = {
  plus: 'M12 5v14M5 12h14', back: 'M15 18l-6-6 6-6', next: 'M9 18l6-6-6-6', photo: 'M4 7h3l2-2.5h6L17 7h3v12H4zM12 16.5a3.5 3.5 0 100-7 3.5 3.5 0 000 7z',
  download: 'M12 4v11M7 10.5l5 5 5-5M5 20h14', note: 'M7 3.5h7l4 4V20.5H7zM14 3.5v4h4M9.5 12h6M9.5 15.5h6', close: 'M6 6l12 12M18 6L6 18',
  search: 'M10.5 17a6.5 6.5 0 100-13 6.5 6.5 0 000 13zM15.5 15.5L20 20', compare: 'M12 3v18M5 7h4v10H5zM15 7h4v10h-4z',
};

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('class', 'zs-icon');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', icons[name]); svg.append(path);
  return svg;
}
function loadDrafts(storage, now = Date.now(), replaceCurrent = false) {
  const state = formDraftState();
  if (!replaceCurrent && state.dirty && state.error) return { ok: false, error: state.error };
  try {
    storage ??= localStorage;
    const raw = storage.getItem('zyb.studio.drafts'), saved = readFormDrafts(raw), next = new Map();
    let partial = false;
    for (const [key, entry] of Object.entries(saved)) {
      if (!/^[\w:.-]{1,200}$/.test(key) || !entry || typeof entry.saved !== 'number' || !Number.isFinite(entry.saved)) { partial = true; continue; }
      if (now - entry.saved >= 14 * 24 * 3600 * 1000) continue;
      const values = cleanDraft(entry.values);
      if (JSON.stringify(values) !== JSON.stringify(entry.values)) partial = true;
      if (values.length) next.set(key, values);
    }
    if (replaceCurrent && partial) throw Error('incomplete form draft');
    drafts.clear();
    for (const [key, values] of next) drafts.set(key, values);
    Object.assign(state, { loaded: true, raw, blocked: partial, dirty: false,
      error: partial ? '部分旧文字草稿未能恢复，未覆盖本机记录。' : '' });
  } catch {
    state.blocked = true; state.error = '旧文字草稿未能读取，未覆盖本机记录。';
  }
  refreshFormDraftNotice();
  return { ok: !state.error, error: state.error };
}
function saveDrafts(storage, now = Date.now(), replaceUnread = false) {
  const state = formDraftState();
  state.dirty = true;
  if (state.blocked) { refreshFormDraftNotice(); return { ok: false, error: state.error }; }
  let previous, raw;
  try {
    storage ??= localStorage;
    raw = storage.getItem('zyb.studio.drafts');
    previous = replaceUnread ? {} : readFormDrafts(raw);
    if (state.loaded && raw !== state.raw) {
      state.blocked = true; state.error = '其他页面更新了本机文字草稿，未覆盖新记录。';
      refreshFormDraftNotice(); return { ok: false, error: state.error };
    }
  } catch {
    state.blocked = true; state.error = '旧文字草稿未能读取，未覆盖本机记录。';
    refreshFormDraftNotice(); return { ok: false, error: state.error };
  }
  const preferenceDefaults = { category: '其他设计', deliverable: '概念效果图', decision: '待客户确认', text_only: false, is_demo: false,
    quality: '精细', size: '2K', aspect: '1:1' };
  const meaningful = values => values.some(([name, value]) => draftText.has(name) &&
    (Array.isArray(value) ? value.length > 0 : typeof value === 'string' && value.trim() !== '') ||
    Object.hasOwn(preferenceDefaults, name) && value !== preferenceDefaults[name]);
  const rows = [...drafts].map(([key, raw], index) => {
    const old = previous?.[key], values = cleanDraft(raw);
    return { key, index, values, saved: old && JSON.stringify(old.values) === JSON.stringify(values) ? old.saved : now };
  }).filter(row => meaningful(row.values)).sort((a, b) => b.saved - a.saved || b.index - a.index).slice(0, 30);
  try {
    const value = JSON.stringify(Object.fromEntries(rows.map(row => [row.key, { values: row.values, saved: row.saved }])));
    storage.setItem('zyb.studio.drafts', value);
    Object.assign(state, { loaded: true, raw: value, blocked: false, dirty: false, error: '' });
  } catch { state.error = '这台设备未能暂存文字填写。'; }
  refreshFormDraftNotice();
  return { ok: !state.error, error: state.error };
}
function cleanDraft(values) {
  if (!Array.isArray(values)) return [];
  return values.filter(row => Array.isArray(row) && row.length === 2 && typeof row[0] === 'string' && /^[a-z][a-z_]{0,40}$/.test(row[0]))
    .map(([name, value]) => [name, Array.isArray(value)
      ? value.filter(pair => Array.isArray(pair) && pair.length === 2 && pair.every(part => typeof part === 'string')) : value])
    .filter(([, value]) => typeof value === 'string' || typeof value === 'boolean' || Array.isArray(value));
}
function formDraftState() {
  if (!drafts.storageState) Object.defineProperty(drafts, 'storageState', {
    value: { loaded: false, raw: null, blocked: false, dirty: false, error: '' } });
  return drafts.storageState;
}
function readFormDrafts(raw) {
  const saved = raw === null || raw === undefined ? {} : JSON.parse(raw);
  if (!saved || typeof saved !== 'object' || Array.isArray(saved)) throw Error('invalid form draft');
  return saved;
}
function refreshFormDraftNotice() {
  if (typeof panel === 'undefined' || !panel) return;
  panel.querySelector('[data-form-draft-notice]')?.refreshDraft?.();
}
function retryFormDrafts() {
  rememberInputs();
  const result = saveDrafts();
  if (result.ok) tell('本机文字草稿已暂存，尚未提交。');
  return result;
}
function rereadFormDrafts() {
  if (!window.confirm('重新读取会替换这个工作台尚未提交的文字和参考选择。服务器记录和历史图片不变。是否继续？'))
    return { ok: false, cancelled: true };
  rememberInputs();
  const result = loadDrafts(undefined, Date.now(), true);
  if (!result.ok) return result;
  const saved = drafts.get((task?.id || 'new') + ':refs')?.[0]?.[1];
  picked = new Map(Array.isArray(saved) ? saved : (task?.references || []).map(ref => [ref.id, ref.role]));
  draftKey = null;
  render();
  tell('已重新读取本机文字草稿，请核对内容；尚未提交。');
  return result;
}
function replaceFormDrafts() {
  const state = formDraftState();
  if (!state.blocked) return retryFormDrafts();
  if (!window.confirm('继续会用本页面保留的全部文字草稿替换这台设备的文字草稿记录；尚未读出的内容无法保留。服务器记录和历史图片不变。是否替换？'))
    return { ok: false, cancelled: true };
  rememberInputs();
  const previous = { loaded: state.loaded, raw: state.raw };
  try {
    const raw = localStorage.getItem('zyb.studio.drafts');
    // Consent permits replacing unread content, not discarding the recoverable in-page draft.
    Object.assign(state, { loaded: true, raw, blocked: false });
    const result = saveDrafts(undefined, Date.now(), true);
    if (!result.ok) { Object.assign(state, previous, { blocked: true }); refreshFormDraftNotice(); }
    else tell('本机文字草稿已暂存，尚未提交。');
    return result;
  } catch {
    state.blocked = true; state.dirty = true; state.error = '本机文字草稿仍无法读取，当前填写已保留在页面内。';
    refreshFormDraftNotice(); return { ok: false, error: state.error };
  }
}
function formDraftNotice() {
  const notice = el('section', { class: 'zs-upload-recovery', 'data-form-draft-notice': '1',
    'aria-label': '文字草稿暂存', 'aria-live': 'polite', hidden: true });
  notice.refreshDraft = () => {
    const state = formDraftState();
    notice.hidden = !state.error;
    if (!state.error) { notice.replaceChildren(); return; }
    notice.replaceChildren(el('p', { class: 'zs-help', role: 'status' }, state.error
      + ' 当前修改只在页面内，刷新或关页可能丢失；本机旧记录未被覆盖。'),
      el('div', { class: 'zs-row' }, state.blocked
        ? [button('重新读取本机文字草稿', rereadFormDrafts, false, false, { class: 'zs-btn quiet small' }),
          button('用当前填写替换本机文字草稿…', replaceFormDrafts, false, false, { class: 'zs-btn quiet small' })]
        : button('重试暂存文字草稿', retryFormDrafts, false, false, { class: 'zs-btn quiet small' })));
  };
  notice.refreshDraft();
  return notice;
}
function rememberInputs() {
  if (!view || !draftKey) return;
  drafts.set(draftKey, [...view.querySelectorAll('input[name],textarea[name],select[name]')]
    .filter(node => !serverOwnedInputs.has(node.name) && node.type !== 'file' && node.type !== 'hidden' &&
      !(node.type === 'radio' && !node.checked) && node.dataset?.draft !== 'off' &&
      !(node.form?.dataset.review && (['design', 'culture', 'process'].includes(node.name) || node.name.startsWith('floor_'))))
    .map(node => [node.name, node.type === 'checkbox' ? node.checked : node.value]));
}
function restoreInputs() {
  for (const [name, value] of drafts.get(draftKey) || []) {
    const nodes = [...view.querySelectorAll(`[name="${name}"]`)].filter(node => node.dataset.draft !== 'off' &&
      !serverOwnedInputs.has(node.name) && !(node.form?.dataset.review &&
        (['design', 'culture', 'process'].includes(node.name) || node.name.startsWith('floor_'))));
    if (nodes[0]?.type === 'radio') {
      const match = nodes.find(node => node.value === value);
      if (match) match.checked = true;
      continue;
    }
    for (const node of nodes) {
      if (node.type === 'checkbox') node.checked = value;
      else if (node.tagName !== 'SELECT' || [...node.options].some(option => option.value === value)) node.value = value;
    }
  }
  for (const node of view.querySelectorAll('[data-sync]')) node.dispatchEvent(new Event('change'));
  updateActionButtons();
}
function valueOf(name, fallback = '') {
  const nodes = [...(view?.querySelectorAll(`[name="${name}"]`) || [])];
  const node = nodes[0]?.type === 'radio' ? nodes.find(n => n.checked) : nodes[0];
  return node?.value || fallback;
}
function updateActionButtons() {
  for (const node of view?.querySelectorAll('[data-paid]') || []) {
    const ready = node.dataset.paid === 'check' ? status.vision_ready : status.image_ready;
    const missing = node.checkPrerequisites?.();
    node.disabled = busy() || ready === false || Boolean(task?.job?.local_recovery?.length || task?.job?.result_recovery?.length) || tasks.some(row => row.id !== task?.id && row.job?.status === 'running') || node.dataset.unavailable === '1' || Boolean(missing);
  }
}
function paidButton(text, run, mode = 'image', unavailable = false) {
  const node = button(text, async () => {
    const ready = mode === 'check' ? status.vision_ready : status.image_ready;
    if (busy() || unavailable || ready === false || task?.job?.local_recovery?.length || task?.job?.result_recovery?.length || tasks.some(row => row.id !== task?.id && row.job?.status === 'running') || node.checkPrerequisites?.()) return;
    generationPending = true;
    updateActionButtons();
    try {
      return await run({});
    } finally {
      generationPending = false;
      if (panel?.open) { render(); schedule(); }
    }
  }, true, true);
  node.dataset.paid = mode;
  node.dataset.unavailable = unavailable ? '1' : '0';
  return node;
}
function generationControl(node, issue) {
  const hint = el('p', { class: 'zs-action-hint', id: 'zs-action-' + (++actionHintId), role: 'status' });
  node.setAttribute('aria-describedby', hint.id);
  node.checkPrerequisites = () => {
    const message = issue();
    if (hint.textContent !== message) hint.textContent = message;
    hint.hidden = !message;
    return message;
  };
  return el('div', { class: 'zs-generation-control' }, hint, node);
}

function userMessage(message) {
  const text = String(message || '操作没有完成，请稍后再试。');
  if (/^空间识别服务暂时不可用[（(]\d{3}[）)]$/.test(text))
    return '空间识别暂未完成，平面图和参考照片已保留。可以稍后继续生成。';
  if (/限流/.test(text)) return '出图服务繁忙，请稍后再试未完成的图片。';
  if (/密钥|API.?key|unauthorized|authentication/i.test(text)) return '服务连接异常，请联系管理员检查配置。';
  if (/余额|额度|配额|费用|收费|付费|计费|扣费|价格|quota|insufficient.balance|billing|payment|credits?/i.test(text)) return '服务暂不可用，请联系管理员处理。';
  if (/\/Users\/|\/home\/|Traceback|https?:\/\//i.test(text)) return '服务暂时不可用，已有内容都还在。请联系管理员检查。';
  if (/已有设计任务运行/.test(text)) return '另一份设计正在生成，等它完成后再试。';
  if (/本任务正在运行/.test(text)) return '这份设计正在处理，完成或停止后再改。';
  return text.replace(/(?:doubao[-\w.]*|deepseek[-\w.]*|seedream(?:\s+[\d.]+)?(?:\s+pro)?|qwen[-\w.]*|flux[-\w.]*|ComfyUI|火山引擎)/gi, '服务')
    .replace(/模型/g, '服务').replace(/\bAPI\b/g, '服务');
}
function localJob(job) { return job.action === 'export' || !cloudImages(); }
function jobMessage(job) {
  if (job.result_recovery?.length && job.status !== 'running') return (job.registration_error ? userMessage(job.registration_error) + ' ' : '')
    + '图片已保存，但还没登记成设计版本。可以恢复已有结果，不会重新生图。'
    + (job.status === 'uncertain' || job.cloud_pending ? ' 另有请求尚未确认，请联系管理员核查。' : '');
  if (job.local_recovery?.length && job.status !== 'running') return job.local_error ? userMessage(job.local_error) : '图片已生成，合成还没完成。可以继续处理，不会重新生图。';
  const items = Object.values(job.items || {});
  const count = state => items.filter(value => value === state).length;
  if (job.status === 'running') {
    if (job.phase === 'layout') return job.cancel ? '正在结束空间识别，识别进度会保留，不再开始生图。'
      : '正在识别空间并安排视角，平面图已保存。关掉页面也会继续。';
    if (job.cancel) return job.action === 'plan' ? '正在停止构思…'
      : localJob(job) ? '正在停止：排队中的图片会取消；正在处理的图片成功完成后会保留。' : '不再开始剩余图片；已发出的请求仍会继续，成功返回的图片会保留。';
    if (job.action === 'plan') return (job.render ? '正在构思三个方案，构思完会接着出图' : '正在构思方案') + '…关掉页面也会继续。';
    const name = job.phase === 'interior-views' ? '正在生成室内效果'
      : { generate: '正在出图', design: '正在生成参考方案', compose: '正在组合图片', refine: '正在修改图片', export: '正在导出高清图', check: '正在检查图片', 'recover-results': '正在恢复已有图片的登记' }[job.action] || '正在处理';
    const total = items.length || job.total || 1, done = items.length ? count('done') : job.completed || 0;
    return name + (total > 1 ? `（已完成 ${done}/${total}）` : '') + '…关掉页面也会继续。';
  }
  if (job.cancel && job.status === 'cancelled') return job.action === 'plan' ? '已停止构思。' : '已停止，做完的图片都保留着。';
  if (job.status === 'uncertain') return job.action === 'recover-results'
    ? '已有图片已恢复登记，另有请求的结果尚未确认。请联系管理员核查，避免重复生成。'
    : uncertainMessage(job);
  if (job.status === 'failed' && count('failed'))
    return [`${count('failed')} 张没有生成${/限流/.test(job.message || '') ? '（服务繁忙）' : ''}`, count('done') ? '其余的已经保留' : '',
      count('skipped') ? `还有 ${count('skipped')} 张没有开始` : ''].filter(Boolean).join('，') + '。' +
      (job.message ? userMessage(job.message) + ' ' : '') + '请先核对失败原因，再重试未完成的步骤。';
  if (job.status === 'done') return '已完成。';
  if (job.status === 'cancelled') return '已停止，做完的图片都保留着。';
  if (job.status === 'interrupted') return '处理中断了，做完的部分都保留着，可以重新开始。';
  return userMessage(job.message);
}
function uncertainMessage(job) {
  if (job.cloud_operation === 'plan') return '上次的构思请求可能已完成，但方案还没登记。请先联系管理员核实，避免重复提交。';
  if (job.cloud_operation === 'vision') return '上次的视觉核对结果还没确认，图片没有改动。请先联系管理员核实服务记录，避免重复核对。';
  return '上次的图片可能已经生成，但这里还没登记上。请先联系管理员核实，避免重复生成。';
}
function stopControl(job) {
  if (job.status !== 'running' || job.cancel || job.action === 'check') return null;
  if (job.phase === 'layout') return ['停止后续生成', '当前识别完成后会保留进度，不再开始生图。'];
  if (job.action === 'plan') return ['停止构思', '正在停止构思。'];
  if (localJob(job)) return ['停止', '排队中的图片会取消；正在处理的图片成功完成后会保留。'];
  const remaining = job.items ? Object.values(job.items).filter(state => state === 'queued').length : (job.total || 1) - (job.completed || 0) - 1;
  return remaining > 0 ? [`停止剩下的 ${remaining} 张`, '不再开始剩余图片；已发出的请求仍会继续，成功返回的图片会保留。'] : null;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'disabled' || key === 'checked' || key === 'hidden') node[key] = value === '' || Boolean(value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) if (child != null && child !== false) node.append(child instanceof Node ? child : document.createTextNode(child));
  return node;
}
function button(text, run, primary = false, disabled = busy(), extra = {}) {
  return el('button', { class: 'zs-btn' + (primary ? ' primary' : ''), type: 'button', disabled, ...extra,
    onclick: async () => { try { await run(); } catch (error) { tell(error.message, true); } } }, text);
}
function tell(message, error = false) {
  const box = panel.querySelector('.zs-notice');
  const display = userMessage(message);
  if (!box.hidden && box.dataset.message === display && box.dataset.error === (error ? '1' : '0')) return;
  box.dataset.message = display;
  clearTimeout(noticeTimer);
  box.replaceChildren(el('span', {}, display),
    el('button', { class: 'zs-icon-btn', type: 'button', 'aria-label': '关闭提示', onclick: () => { box.hidden = true; } }, icon('close')));
  box.dataset.error = error ? '1' : '0'; box.hidden = false;
  box.dataset.validation = error && ['请至少添加两张参考图。', '请说明各张图取用什么、想怎样组合。',
    '请填写本次修改内容', '请填写本次修改内容。', '请先选择并确认修改范围。', '请填写比较部位。'].includes(message) ? message : '';
  box.dataset.context = editContext();
  box.setAttribute('role', error ? 'alert' : 'status');
  if (!error) noticeTimer = setTimeout(() => { box.hidden = true; }, 9000);
}
function hideNotice() {
  const box = panel?.querySelector('.zs-notice');
  if (box) box.hidden = true;
  clearTimeout(noticeTimer);
}
function editContext() { return `${task?.id || ''}:${stage}:${compositionTask === task?.id ? 'compose' : activeImageId || ''}`; }
function refreshValidationNotice() {
  const box = panel?.querySelector('.zs-notice'), message = box?.dataset.validation;
  if (!message || box.hidden) return;
  if (box.dataset.context !== editContext()) return hideNotice();
  const missing = message.includes('两张') ? currentEdit({ id: 'compose' }).rows.length < 2
    : message.includes('修改范围') ? !currentEdit({ id: activeImageId }).selection_proof
    : message.includes('比较部位') ? !valueOf('variant_target').trim() : !valueOf('change').trim();
  if (!missing) hideNotice();
}
async function api(path, body) {
  let res, json;
  try {
    res = await fetch(base + path, { method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json', 'X-Zyb-Studio': '1' },
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(30000) });
  } catch {
    const error = Error('暂时连不上服务，请稍后核对提交状态。页面上填写的内容都还在。');
    error.submissionUncertain = body !== undefined;
    throw error;
  }
  try { json = await res.json(); }
  catch {
    const error = Error(res.status >= 500 ? `服务异常（${res.status}），请稍后刷新核对。已有内容未删除。` : '服务返回内容不完整，请刷新核对已保存的内容。');
    error.submissionUncertain = body !== undefined && (res.ok || res.status >= 500);
    throw error;
  }
  if (!res.ok) {
    const error = Error(res.status >= 500 ? '服务暂时不可用，请稍后核对提交状态。' : userMessage(json.error));
    error.submissionUncertain = body !== undefined && res.status >= 500;
    throw error;
  }
  return json;
}
async function loadTasks() {
  const request = ++listRequest;
  const result = await api('tasks');
  if (request !== listRequest) return false;
  tasks = result.tasks; return true;
}
async function loadAcceptedTasks(owner, revision) {
  try { if (await loadTasks()) return true; } catch {}
  pendingSubmissions.set(owner, Math.max(revision, pendingSubmissions.get(owner) ?? revision));
  return false;
}
function currentTask(id, epoch) { return panel?.open && navigation === epoch && task?.id === id; }
function supersedeRouteLoad() {
  if (routeLoadEpoch !== navigation) return;
  ++navigation; routeLoadEpoch = null; clearTimeout(pollTimer); schedule();
}
async function chooseTask(id, { push = false } = {}) {
  closeInteriorPhotos();
  const epoch = ++navigation;
  routeLoadEpoch = epoch; routePush = false;
  clearTimeout(pollTimer);
  let next;
  try { next = await api('tasks/' + id); }
  catch (error) {
    if (epoch !== navigation || !panel?.open) return false;
    routeLoadEpoch = null; routePush = false; syncRoute(); schedule();
    throw Error('未能打开设计，仍在原页面。' + error.message, { cause: error });
  }
  if (epoch !== navigation || !panel?.open) return false;
  if (!imageSelection.has(id)) {
    try { imageSelection.set(id, sessionStorage.getItem('zyb.studio.base.' + id)); } catch {}
  }
  if (!next.images?.some(image => image.id === imageSelection.get(id))) imageSelection.delete(id);
  closeInteriorPhotos();
  galleryOpen = false;
  task = next;
  try { compositionTask = sessionStorage.getItem('zyb.studio.compose.' + id) === '1' ? id : null; } catch { compositionTask = null; }
  if (task.workspace === 'reference_design' && !task.concepts.length) compositionTask = id;
  picked = new Map(task.references.map(r => [r.id, r.role])); resetReferences();
  stage = task.selected || imageSelection.has(id) || compositionTask === id
    || (task.edit_assets?.length && !(task.interior_input && task.concepts.length)) ? 3 : task.concepts.length ? 2 : 1;
  routeLoadEpoch = null; routePush = push;
  render(); schedule(); return true;
}
async function action(name, options = {}) {
  const id = task.id, epoch = navigation, submittedRevision = task.revision;
  const createsImages = ['generate', 'refine', 'compose', 'design'].includes(name) || name === 'plan' && options.render;
  if (createsImages || name === 'plan' || name === 'check') {
    const connected = await api('status');
    if (!currentTask(id, epoch)) return false;
    if (name === 'plan' && (connected.planner_ready !== true || typeof connected.local_only !== 'boolean')
        || createsImages && (connected.image_ready !== true || !['local', 'ark'].includes(connected.image_provider))
        || name === 'check' && connected.vision_ready !== true) {
      throw Error('生成服务状态尚未确认，未发送本次请求。请稍后再试，已填写的内容仍保留。');
    }
    status = connected;
    options = { ...options };
    for (const key of ['cloud_consent', 'cloud_consent_provider', 'vision_consent', 'plan_consent']) delete options[key];
  }
  const remotePlan = name === 'plan' && status?.local_only === false;
  const cloudHistory = task.cloud_job_history || [];
  const historyDamaged = !Array.isArray(cloudHistory) || cloudHistory.some(row => !row || typeof row !== 'object' || Array.isArray(row));
  if ((createsImages || remotePlan || name === 'check') && historyDamaged)
    throw Error('部分生成记录未能读取，已有图片仍可查看和保存。请联系管理员核对后再生成。');
  const pendingCloud = (Array.isArray(cloudHistory) ? cloudHistory : [])
    .filter(row => row && typeof row === 'object' && !Array.isArray(row) && !row.continued_at);
  if (task.job?.status === 'uncertain' && !pendingCloud.some(row => task.job.operation_id && row.operation_id === task.job.operation_id)) pendingCloud.push(task.job);
  if ((['generate', 'refine', 'compose', 'design', 'check'].includes(name) || (name === 'plan' && options.render) || remotePlan) && pendingCloud.length) {
    const descriptions = new Map();
    for (const row of pendingCloud) {
      const description = uncertainMessage(row);
      descriptions.set(description, (descriptions.get(description) || 0) + 1);
    }
    const details = [...descriptions].map(([description, count]) => `${count} 次：${description}`).join('\n');
    const message = `有 ${pendingCloud.length} 次请求尚未收到完整结果。\n${details}\n\n要开始一次新的生成吗？已有图片会保留，旧请求不会自动重试。`;
    if (!window.confirm(message)) return false;
    options.acknowledge_uncertain = true;
  }
  const remoteImage = status?.image_provider === 'ark' && (['generate', 'refine', 'compose', 'design'].includes(name) || name === 'plan' && options.render);
  const remoteCheck = name === 'check';
  if (remotePlan) options.plan_consent = true;
  if (remoteImage) { options.cloud_consent = true; options.cloud_consent_provider = status.image_provider; }
  if (remoteCheck) options.vision_consent = true;
  let next;
  try { next = await api('tasks/' + id + '/' + name, { revision: task.revision, ...options }); }
  catch (error) {
    if (error.submissionUncertain && ['plan', 'generate', 'refine', 'compose', 'design', 'export', 'check', 'finish-local', 'recover-results'].includes(name)) {
      pendingSubmissions.set(id, submittedRevision);
      try {
        const fresh = await api('tasks/' + id);
        if (fresh.revision > pendingSubmissions.get(id)) pendingSubmissions.delete(id);
        if (currentTask(id, epoch) && fresh.revision >= task.revision) task = fresh;
      } catch {}
      if (panel?.open) { render(); schedule(); }
      if (!currentTask(id, epoch)) return false;
      throw Error(pendingSubmissions.has(id) ? '提交结果尚未确认，正在核对服务器记录。确认前不会再次生成。'
        : '已重新读取服务器记录，请核对进度与结果。本次请求不会自动重发。');
    }
    if (!currentTask(id, epoch)) return false;
    if (name === 'cancel' && /停止对应的处理批次|停止按钮已更新/.test(error.message)) {
      let refreshed = false;
      try {
        const fresh = await api('tasks/' + id);
        if (currentTask(id, epoch) && fresh.revision >= task.revision) {
          task = fresh; render(); schedule(); refreshed = true;
        }
      } catch {}
      if (!currentTask(id, epoch)) return false;
      throw Error('本次停止未生效，当前任务没有改动。' + (refreshed
        ? '页面已重新读取，请确认当前处理后再操作。' : '请重新读取记录后再操作。'));
    }
    if (!/已更新，请刷新后重试|另一窗口更新/.test(error.message)) throw error;
    try { const fresh = await api('tasks/' + id); if (currentTask(id, epoch) && fresh.revision >= task.revision) { task = fresh; render(); schedule(); } } catch {}
    throw Error('这份设计刚被别处改过，页面已更新。请确认后再操作一次。');
  }
  if (!currentTask(id, epoch)) {
    if (panel?.open && task?.id === id && next.revision >= task.revision) { task = next; render(); }
    await loadAcceptedTasks(id, submittedRevision);
    schedule();
    return false;
  }
  if (next.revision >= task.revision) task = next;
  if (name === 'references' || name === 'interior-input' && options.references) {
    picked = new Map(task.references.map(r => [r.id, r.role])); rememberPicks();
  }
  if (name === 'feedback' && view?.querySelector('.zs-feedback [name="comment"]')?.value === options.comment &&
      view?.querySelector('.zs-feedback [name="decision"]')?.value === options.decision) {
    for (const [name, value] of [['decision', '待客户确认'], ['comment', '']]) {
      const input = view?.querySelector(`.zs-feedback [name="${name}"]`);
      if (input) input.value = value;
    }
  }
  const listUnavailable = !await loadAcceptedTasks(id, submittedRevision);
  if (listUnavailable && currentTask(id, epoch)) tasks = task.hidden ? tasks.filter(row => row.id !== task.id)
    : [task, ...tasks.filter(row => row.id !== task.id)];
  if (!currentTask(id, epoch)) return false;
  render(); schedule();
  if (listUnavailable) tell('本次操作已提交，当前设计已更新；设计列表暂未刷新。', true);
  return name === 'dimensions' ? structuredClone(next.images.find(row => row.id === options.image_id)?.dimensions) : true;
}
async function recoverResults() {
  if (busy()) return false;
  generationPending = true;
  render();
  try {
    return await action('recover-results');
  } finally {
    generationPending = false;
    if (panel?.open) { render(); schedule(); }
  }
}
async function startPlan(useReferences, options = {}) {
  if (busy()) return;
  if (useReferences && !picked.size) throw Error('还没有选参考素材。也可以一份都不选，直接出方案。');
  const references = useReferences ? [...picked].map(([id, role]) => {
    const saved = task.references.find(ref => ref.id === id);
    return saved?.role === role ? { ...saved, id, role } : { id, role,
      ...(saved?.source_selection ? { source_selection: saved.source_selection, source_sha256: saved.source_sha256 } : {}) };
  }) : [];
  planning = true;
  render();
  try {
    if (!await action('references', { references })) return;
    if (await action('plan', { ...options })) { drafts.delete(task.id + ':refs'); stage = 2; }
  } finally {
    planning = false;
    render(); schedule();
  }
}
function schedule() {
  clearTimeout(pollTimer);
  if (!panel?.open || !busy() && !tasks.some(row => row.job?.status === 'running')) return;
  const id = task?.id, epoch = navigation;
  const current = () => panel?.open && navigation === epoch && task?.id === id;
  pollTimer = setTimeout(async () => {
    try {
      let changed = false;
      const reconciled = new Set();
      const accepted = [];
      for (const [owner, revision] of pendingSubmissions) {
        const next = await api('tasks/' + owner);
        if (next.revision > revision) accepted.push([owner, revision]);
        if (current() && owner === id && next.revision >= task.revision) { task = next; changed = true; reconciled.add(owner); }
      }
      if (id && task.job?.status === 'running' && !reconciled.has(id)) {
        const next = await api('tasks/' + id);
        if (!current()) return;
        if (next.revision > task.revision) { task = next; changed = true; }
      }
      const before = JSON.stringify(tasks);
      const published = await loadTasks();
      if (published) for (const [owner, revision] of accepted)
        if (pendingSubmissions.get(owner) === revision) pendingSubmissions.delete(owner);
      if (current() && (changed || before !== JSON.stringify(tasks))) render();
    } catch { if (current()) tell('连接中断了，恢复后会继续更新。已经保存的内容不受影响。', true); }
    if (current()) schedule();
  }, document.hidden ? 15000 : 3000);
}
async function endSubmissionWait(owner) {
  if (!pendingSubmissions.has(owner) || pendingChecks.has(owner)) return;
  const revision = pendingSubmissions.get(owner);
  pendingChecks.add(owner);
  if (panel?.open) render();
  try {
    const fresh = await api('tasks/' + owner);
    await loadTasks();
    if (pendingSubmissions.get(owner) !== revision) return;
    if (fresh.revision <= revision && fresh.job?.status !== 'running' && !window.confirm(
      '服务器暂未登记这次提交，但请求仍可能已发送。这里只结束页面等待，不会撤回或重发请求。请先核对服务端进度和云端记录；有疑问请联系管理员。已经核对，要结束等待吗？')) return;
    if (task?.id === owner && fresh.revision >= task.revision) task = fresh;
    pendingSubmissions.delete(owner);
    if (panel?.open) tell(fresh.job?.status === 'running' ? '已找回服务器进度，正在处理；没有重发请求。'
      : '已结束页面等待，没有重发或撤回请求。开始新生成前，请再核对进度和已有结果。');
  } finally {
    pendingChecks.delete(owner);
    if (panel?.open) { render(); schedule(); }
  }
}
function parseRoute(address) {
  const parts = String(address || '').replace(/^#?\/?/, '').split('/');
  if (parts[0] === 'library' && parts.length <= 2) return { view: 'library', tab: parts[1] === 'refs' ? 'refs' : 'archive' };
  if (parts[0] === 'new' && parts[1] === 'refs' && parts.length === 2) return { view: 'new-refs' };
  if (parts[0] === 'design' && /^[a-f0-9]{32}$/.test(parts[1] || '') && parts.length <= 4) {
    const at = routeTabs.indexOf(parts[2]);
    if (parts.length === 4 && (![3, 4].includes(at) || !/^[a-f0-9]{32}$/.test(parts[3]))) return { view: 'home' };
    return { view: 'design', id: parts[1], stage: at >= 0 ? at : null, ...(parts[3] ? { imageId: parts[3] } : {}) };
  }
  return { view: 'home' };
}
function routeFor(state) {
  if (state.task) return `design/${state.task.id}/${routeTabs[state.stage] || 'brief'}` +
    ([3, 4].includes(state.stage) && /^[a-f0-9]{32}$/.test(state.imageId || '') ? '/' + state.imageId : '');
  if (state.stage === 5) return 'library/' + (state.libraryTab === 'refs' ? 'refs' : 'archive');
  if (state.stage === 1) return 'new/refs';
  return '';
}
function routeState(url, replace) {
  const current = history.state && typeof history.state === 'object' ? history.state : {};
  const position = Number.isInteger(current.position) ? current.position : 0;
  return { ...current, back: replace ? current.back ?? null : current.current ?? null, current: url, forward: null,
    position: replace ? position : position + 1, replaced: Boolean(replace), scroll: null };
}
function syncRoute() {
  if (routeLoadEpoch === navigation) return;
  const composing = typeof compositionTask !== 'undefined' && compositionTask === task?.id;
  const address = routeFor({ task, stage, libraryTab, imageId: (galleryOpen || composing) && stage === 3 ? null : imageSelection.get(task?.id) });
  if (placeOf(location.search) === address) { routePush = false; return; }
  const params = new URLSearchParams(location.search); params.delete('at');
  const query = [params.toString(), address ? 'at=' + address : ''].filter(Boolean).join('&');
  const url = location.pathname + (query ? '?' + query : '') + location.hash;
  history[routePush ? 'pushState' : 'replaceState'](routeState(url, !routePush), '', url);
  routePush = false;
}
function placeOf(search) { return new URLSearchParams(search).get('at') || ''; }
function designStage(value) { return task?.interior_input && value === 1 ? 2 : value; }
function go(next) {
  closeInteriorPhotos(); ++navigation; clearTimeout(pollTimer);
  routePush = true; hideNotice(); stage = designStage(next); comparing = false; render(); schedule();
}
async function applyRoute(route) {
  closeInteriorPhotos();
  ++navigation; clearTimeout(pollTimer);
  routePush = false;
  if (route.view === 'design' && tasks.some(row => row.id === route.id)) {
    if (task?.id !== route.id && !await chooseTask(route.id)) return;
    if (route.imageId && task.images.some(image => image.id === route.imageId)) { endComposition(); imageSelection.set(task.id, route.imageId); galleryOpen = false; }
    if (route.stage != null && (route.stage === 1 || designTabs().some(([target]) => target === route.stage))) { stage = designStage(route.stage); comparing = false; render(); }
    schedule();
    return;
  }
  if (route.view === 'library') { libraryTab = route.tab; return openLibrary(); }
  goHome();
  if (route.view === 'new-refs') { stage = 1; render(); }
}
function field(key, value = '', options = null, label = labels[key] || key) {
  const multiline = ['goal', 'change', 'comment'].includes(key);
  const control = options ? el('select', { name: key, disabled: uploading || planning }, options.map(x => el('option', { value: x }, x)))
    : el(multiline ? 'textarea' : 'input', { name: key, autocomplete: 'off', disabled: uploading || planning, rows: multiline ? '4' : null,
      type: multiline ? null : 'text', maxlength: key === 'title' ? '150' : key === 'reviewer' ? '100' : '1200' });
  control.value = value;
  return el('label', { class: 'zs-field' }, el('span', {}, label), control);
}
function segmented(name, value, options, legend, extra = {}) {
  return el('fieldset', { class: 'zs-seg-field', ...extra }, el('legend', {}, legend),
    el('div', { class: 'zs-seg' }, options.map(([option, text]) => el('label', {},
      el('input', { type: 'radio', name, value: option, checked: option === value, disabled: uploading || planning }), el('span', {}, text)))));
}
function heading(title, description, kicker) {
  return el('div', { class: 'zs-heading' }, kicker ? el('p', { class: 'zs-kicker' }, kicker) : null,
    el('h2', {}, title), description ? el('p', {}, description) : null);
}
function empty(title, description, ...actions) {
  return el('div', { class: 'zs-empty' }, el('h3', {}, title), description ? el('p', {}, description) : null,
    actions.length ? el('div', { class: 'zs-row center' }, actions) : null);
}
function newTask() {
  closeInteriorPhotos();
  finishCreation();
  ++navigation; clearTimeout(pollTimer); task = null; stage = 0; resetReferences(); picked.clear();
  for (const key of ['new:0', 'new:refs', 'new:photo']) drafts.delete(key);
  clearPending('new'); draftKey = null; render();
}
function clearPending(key) {
  const item = pendingUploads.get(key);
  if (item?.preview) URL.revokeObjectURL(item.preview);
  for (const preview of item?.previews || []) URL.revokeObjectURL(preview);
  pendingUploads.delete(key);
  if (key === 'new') drafts.delete('new:photo');
}
function goHome() {
  closeInteriorPhotos();
  hideNotice(); ++navigation; clearTimeout(pollTimer); task = null; stage = 0; resetReferences(); picked.clear(); comparing = false;
  render();
  const epoch = navigation;
  loadTasks().then(() => { if (epoch === navigation && panel?.open) { renderProjectList(); schedule(); } }).catch(() => {});
}
function validateUpload(file) {
  if (!file || !file.size) throw Error('请先选一张照片。');
  if (file.size > (status.upload_max_bytes || 20 * 1024 * 1024)) throw Error('照片不能超过 20 MB，可以先缩小或另存为 JPG。');
  if (!/\.(jpe?g|png|webp)$/i.test(file.name)) throw Error('请选择 JPG、PNG 或 WebP 格式的照片。iPhone 的 HEIC 照片可以先导出为 JPG。');
}
async function uploadImage(id, revision, item) {
  validateUpload(item.file);
  const query = new URLSearchParams({ revision, mode: item.mode, name: item.file.name });
  let response, value;
  try {
    response = await fetch(base + 'tasks/' + id + '/upload?' + query, { method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream', 'X-Zyb-Studio': '1' }, body: item.file, signal: AbortSignal.timeout(90000) });
    value = await response.json();
  } catch { throw Object.assign(Error('上传时连接中断了，照片还在，可以再传一次。'), { uploadUncertain: true }); }
  if (!response.ok) throw Error(userMessage(value.error));
  return value;
}
async function matchingUpload(saved, item) {
  if (!globalThis.crypto?.subtle) return null;
  item.sha256 ||= [...new Uint8Array(await crypto.subtle.digest('SHA-256', await item.file.arrayBuffer()))]
    .map(byte => byte.toString(16).padStart(2, '0')).join('');
  return saved.images.find(image => image.kind === 'upload' && image.source_sha256 === item.sha256 &&
    image.upload_mode === item.mode && image.concept_id === saved.selected) || null;
}
function choosePhoto(key, file) {
  const files = Array.isArray(file) ? file : [file];
  files.forEach(validateUpload);
  if (files.length > 10) throw Error('一次最多添加10张参考照片。');
  hideNotice();
  const mode = pendingUploads.get(key)?.mode || (key === 'new' ? '' : 'scene');
  clearPending(key);
  pendingUploads.set(key, { file: files[0], files, mode });
  if (key === 'new') drafts.set('new:photo', [['photo', files.map(item => item.name).join('、')]]);
  render();
  view.querySelector('.zs-compose [name=goal]')?.focus({ preventScroll: true });
}
function fileInput(key, label) {
  const input = el('input', { type: 'file', class: 'zs-file', accept: '.jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp',
    'aria-label': label, disabled: busy(), multiple: key === 'new' });
  input.addEventListener('change', () => {
    const file = input.files[0];
    if (!file) return;
    try { choosePhoto(key, key === 'new' ? [...input.files] : file); } catch (error) { input.value = ''; tell(error.message, true); }
  });
  return input;
}
function photoPicker(key) {
  const item = pendingUploads.get(key);
  if (!item) return el('label', { class: 'zs-drop' }, fileInput(key, '选择照片'), el('span', { class: 'zs-drop-icon' }, icon('photo')),
    el('strong', {}, '添加照片'), el('span', {}, '拖到这里，或点一下选择'), el('small', {}, 'JPG、PNG、WebP，20 MB 以内'));
  item.preview ||= URL.createObjectURL(item.file);
  const mode = segmented('upload_mode', item.mode, key === 'new'
    ? [['scene', '在这张图上修改'], ['reference', '参考图片做新方案']]
    : [['scene', '现场照片'], ['edit', '已有设计图']], key === 'new' ? '你想怎么用图片？' : '这张图是');
  const hint = el('p', { class: 'zs-help' });
  const sync = () => {
    const value = mode.querySelector('input:checked')?.value || '';
    item.mode = value;
    hint.textContent = !value ? '先选图片用途。上传照片不会自动开始生成。'
      : value === 'reference' ? '取用图片的材质、造型或风格，做一个新设计。下一步可设置用途、取用范围和方案数量。'
      : (item.files?.length || 1) > 1 ? '直接改图一次只用一张底图。请换选一张，或选择参考图片做新方案。'
      : '直接在这张现场照片或已有设计图上修改，原图保留。';
  };
  for (const radio of mode.querySelectorAll('input')) radio.dataset.draft = 'off';
  mode.addEventListener('change', () => { sync(); if (key === 'new') render(); }); sync();
  item.previews ||= (item.files || [item.file]).map(file => URL.createObjectURL(file));
  return el('div', { class: 'zs-photo' },
    item.previews.length > 1 ? el('div', { class: 'zs-new-photo-grid' }, item.previews.map((src, i) =>
      el('figure', {}, el('img', { src, alt: `待上传的参考图 ${i + 1}` }), el('figcaption', {}, `参考图 ${i + 1}`))))
      : el('div', { class: 'zs-photo-frame' }, el('img', { src: item.preview, alt: '待上传的照片：' + item.file.name })),
    el('div', { class: 'zs-photo-bar' }, el('span', { class: 'zs-file-name', title: item.file.name }, item.file.name),
      el('label', { class: 'zs-btn quiet small' }, fileInput(key, '换一张照片'), key === 'new' ? '重新选择' : '换一张'),
      button('移除', () => { clearPending(key); render(); }, false, busy(), { class: 'zs-btn quiet small' })),
    mode, hint);
}
function uploadPanel(title, description) {
  const key = task.id, item = pendingUploads.get(key);
  const upload = button('上传这张照片', async () => {
    if (busy()) return;
    const value = pendingUploads.get(key); validateUpload(value?.file);
    const id = task.id, epoch = navigation;
    uploading = true; render();
    try {
      if (value.references?.length) {
        if (!await action('references', { references: value.references })) return;
        delete value.references;
      }
      let saved;
      try { saved = await uploadImage(id, task.revision, value); }
      catch (error) {
        const fresh = await api('tasks/' + id).catch(() => null);
        if (!fresh || !error.uploadUncertain || !await matchingUpload(fresh, value)) {
          if (fresh && currentTask(id, epoch) && fresh.revision >= task.revision) task = fresh;
          throw /已更新，请刷新后重试|另一窗口更新/.test(error.message) ? Error('这份设计刚被别处改过，页面已更新，请再上传一次。') : error;
        }
        saved = fresh;
      }
      clearPending(key);
      if (currentTask(id, epoch)) {
        task = saved; stage = 3; imageSelection.set(id, saved.concepts.find(c => c.id === saved.selected).source_image_id);
        await loadTasks();
        if (currentTask(id, epoch)) await startPhotoEdit(task.brief.goal, value.pictures || []);
      }
    } finally { uploading = false; if (currentTask(id, epoch)) render(); }
  }, true, busy() || !item);
  return el('section', { class: 'zs-upload', 'aria-label': title }, el('h3', {}, title), el('p', { class: 'zs-help' }, description),
    photoPicker(key), item ? el('div', { class: 'zs-row' }, upload) : null);
}
async function submitNew(form, next) {
  if (busy()) return;
  const epoch = navigation;
  const values = Object.fromEntries(new FormData(form));
  const image = pendingUploads.get('new');
  if (image) validateUpload(image.file);
  if (image && !image.mode) throw Error('请先选择图片用途。');
  if (image?.mode === 'reference') return createReferenceDesign(briefValues(values), image, epoch, [...picked]);
  if (image?.files?.length > 1) throw Error('直接改图一次只用一张底图，请重新选择。');
  return createDesign(briefValues(values), image, planOptions(values), epoch, [...picked]);
}
async function createReferenceDesign(values, image, epoch, references = []) {
  if (busy()) return;
  const files = image.files || [image.file];
  files.forEach(validateUpload);
  let created;
  uploading = true; render();
  try {
    const selected = references.map(([id, role]) => ({ id, role }));
    const details = selected.length ? (await api('references?selected=' + selected.map(ref => ref.id).join(','))).references : [];
    const pictures = details.filter(ref => ref.kind === 'image');
    if (pictures.length + files.length > 10) throw Error('本次参考图片超过 10 张，请减少已选图片或上传照片。');
    if (epoch !== navigation || !panel?.open) return;
    created = await createOnce({ ...values, workspace: 'reference_design' });
    if (epoch !== navigation || !panel?.open) return;
    await loadTasks();
    if (epoch !== navigation || !panel?.open || !await chooseTask(created.id, { push: true })) return;
    picked = new Map(references); rememberPicks();
    const draft = currentEdit({ id: 'compose' });
    draft.designMode = true; draft.count = 3;
    for (const ref of pictures) {
      if (!draft.rows.some(row => row.source === 'reference' && row.id === ref.id))
        addMaterial({ id: 'compose' }, 'reference', ref.id, ref.title, base + 'reference-image/' + ref.id);
      const row = draft.rows.find(row => row.source === 'reference' && row.id === ref.id);
      row.use = referenceUse(selected.find(value => value.id === ref.id).role);
    }
    saveEditDraft({ id: 'compose' });
    drafts.set(created.id + ':3:compose', [['change', values.goal]]);
    beginComposition(true);
    const waiting = { files, imageId: 'compose', ...(selected.length ? { references: selected } : {}) };
    pendingUploads.set(created.id + ':references', waiting);
    if (selected.length && !await action('references', { references: selected })) return;
    delete waiting.references;
    uploading = false;
    await attachReferences(files, { id: 'compose' });
    finishCreation();
    clearPending('new');
    for (const key of ['new:0', 'new:1', 'new:refs', 'new:photo']) drafts.delete(key);
    saveDrafts();
    tell('参考图已添加。确认用途和数量后再生成。');
  } catch (error) {
    // Keep files and the created project available after a partial upload.
    if (created && task?.id === created.id) {
      finishCreation();
      clearPending('new');
      tell('设计已建好，参考资料或照片尚未处理完。已选内容已保留，可在这里继续。', true);
    } else throw error;
  } finally { uploading = false; if (panel?.open) render(); }
}
async function createOnce(values, sourceContext) {
  const signature = JSON.stringify(sourceContext === undefined ? values : [values, sourceContext]);
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem('zyb.studio.creation') || 'null'); } catch {}
  if (saved?.signature !== signature) {
    saved = { signature, id: crypto.randomUUID().replaceAll('-', '') };
    try { sessionStorage.setItem('zyb.studio.creation', JSON.stringify(saved)); } catch {}
  }
  return api('tasks', { ...values, client_request_id: saved.id });
}
function finishCreation() {
  try { sessionStorage.removeItem('zyb.studio.creation'); } catch {}
}
function planOptions(values = {}) {
  if (status.image_ready === false || values.text_only === '1' || values.text_only === true) return {};
  return { render: true, quality: values.quality || '精细', size: values.size || '2K', aspect: values.aspect || '1:1' };
}
function startLabel(render, count) {
  return (count ? '用所选素材' : '') + (render ? '出三个方案和效果图' : '出三个文字方案');
}
function briefValues(values) {
  const brief = { ...values };
  for (const key of ['upload_mode', 'size', 'aspect', 'quality', 'text_only']) delete brief[key];
  return brief;
}
async function startPhotoEdit(change, pictures = []) {
  const concept = task.concepts.find(c => c.id === task.selected);
  const source = task.images.find(image => image.id === concept?.source_image_id);
  if (!source) return;
  imageSelection.set(task.id, source.id);
  draftKey = null;
  drafts.set(task.id + ':3:edit:' + source.id, [['change', change]]); saveDrafts();
  for (const reference of pictures) {
    addMaterial(source, 'reference', reference.id, reference.title, base + 'reference-image/' + reference.id);
    const row = currentEdit(source).rows.find(value => value.source === 'reference' && value.id === reference.id);
    row.use = referenceUse(reference.role);
  }
  if (pictures.length) saveEditDraft(source);
  stage = 3; render();
  tell('照片已上传。确认修改内容和范围后，点击生成效果图。');
}
async function createDesign(values, image, plan, epoch, references = []) {
  if (busy()) return;
  let owner = null, ownerEpoch = epoch;
  uploading = true; render();
  try {
    const selected = references.map(([id, role]) => ({ id, role }));
    const details = image && selected.length ? (await api('references?selected=' + selected.map(row => row.id).join(','))).references : [];
    const pictures = details.filter(row => row.kind === 'image').map(row => ({ ...row, role: selected.find(ref => ref.id === row.id).role }));
    if (image && pictures.length + 1 > 10) throw Error('含底图最多使用 10 张图片，请减少已选参考图片。');
    if (epoch !== navigation || !panel?.open) return;
    if (image) image = { ...image, references: selected, pictures };
    const created = await createOnce(values);
    if (!image) drafts.set(created.id + ':1', [['text_only', plan?.render !== true],
      ['quality', plan?.quality || '精细'], ['size', plan?.size || '2K'], ['aspect', plan?.aspect || '1:1']]);
    let uploadError;
    if (image) {
      drafts.set(created.id + ':3:edit', [['change', values.goal]]);
      try { await uploadImage(created.id, created.revision, image); clearPending('new'); }
      catch (error) { uploadError = error; pendingUploads.delete('new'); pendingUploads.set(created.id, image); }
    }
    for (const key of ['new:0', 'new:1', 'new:refs', 'new:photo']) drafts.delete(key);
    draftKey = null;
    uploading = false;
    await loadTasks();
    if (epoch !== navigation || !panel?.open || !await chooseTask(created.id, { push: true })) return;
    finishCreation();
    owner = created.id; ownerEpoch = navigation;
    picked = new Map(references); rememberPicks();
    if (uploadError?.uploadUncertain && await matchingUpload(task, image)) { clearPending(created.id); uploadError = null; }
    if (!currentTask(owner, ownerEpoch)) return;
    if (image) {
      const source = task.images.find(row => row.id === task.concepts.find(concept => concept.id === task.selected)?.source_image_id);
      if (source) await startPhotoEdit(values.goal, pictures);
      if (selected.length) {
        if (source) pendingUploads.set(owner + ':references', { files: [], imageId: source.id, references: selected });
        if (!await action('references', { references: selected })) return;
        delete image.references;
        if (source) pendingUploads.delete(owner + ':references');
      }
      if (uploadError) return tell('设计已建好，' + (selected.length ? '已选资料已保存，' : '') + '照片上传未完成：' + uploadError.message, true);
      return;
    }
    if (uploadError) return tell('设计已经建好，照片上传没有完成：' + uploadError.message, true);
    if (!status.planner_ready) return tell('设计已建好。出方案的服务暂时不可用，可以先上传照片直接修改。');
    await startPlan(picked.size > 0, plan);
  } catch (error) {
    if (owner ? currentTask(owner, ownerEpoch) : epoch === navigation && panel?.open)
      tell(owner ? image ? '设计已建好，照片或参考资料尚未处理完，可在这里继续。' : '设计已经建好，但方案没有开始：' + error.message : error.message, true);
  }
  finally { uploading = false; if (panel?.open) render(); }
}
function newForm(editing = false) {
  const photo = !editing && pendingUploads.get('new');
  const imageReady = status.image_ready !== false;
  const form = el('form', { class: editing ? 'zs-form zs-brief-form' : 'zs-form zs-compose zs-form-card' + (photo ? ' has-photo' : ''),
    onsubmit: async event => {
      event.preventDefault();
      try {
        if (!editing) return await submitNew(form, 'plan');
        const changed = await action('brief', { brief: briefValues(Object.fromEntries(new FormData(form))) });
        if (!changed) return;
        stage = task.selected ? 3 : task.concepts.length ? 2 : 1; render();
        tell(task.selected ? '设计要求已更新。' : '设计要求已更新。之前的方案和图片都还在，重新选一个方案就能继续。');
      } catch (error) { tell(error.message, true); }
    } });
  form.addEventListener('keydown', event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); form.requestSubmit ? form.requestSubmit() : form.querySelector('[type=submit]')?.click(); } });
  const directPhoto = photo && photo.mode === 'scene';
  const goal = field('goal', '', null, editing ? '想做什么' : directPhoto ? '想怎么改这张照片？' : '想设计什么？');
  const goalInput = goal.querySelector('textarea');
  goalInput.required = true; goalInput.rows = editing ? 3 : 4;
  goalInput.placeholder = directPhoto ? '例如：把架子上的葫芦换成一尊坐姿铜佛像…' : photo ? '例如：用参考图片中的石材设计一间佛堂，石材用于背景墙和地面…' : '例如：放在玄关的木质小佛龛，线条简洁，底座稳一些…';
  const title = field('title'), category = field('category', '其他设计', status.categories);
  title.querySelector('input').placeholder = '不填会按描述自动起个短名';
  const demo = el('input', { type: 'checkbox', name: 'is_demo', value: '1', disabled: busy() });
  demo.checked = editing && task.is_demo;
  const textOnly = el('input', { type: 'checkbox', name: 'text_only', value: '1', disabled: busy(), 'data-sync': '1' });
  const output = !editing && !photo && imageReady && status.planner_ready ? el('div', { class: 'zs-new-output' },
    el('p', { class: 'zs-field-label' }, '图片尺寸和比例'), settings().box,
    el('label', { class: 'zs-check' }, textOnly, el('span', {}, '先只出文字方案，图片以后再出'))) : null;
  const more = el('details', { class: 'zs-details' }, el('summary', {}, '更多要求', el('span', { class: 'zs-optional' }, '选填')),
    el('div', { class: 'zs-form-grid' }, ...['materials', 'setting', 'audience', 'dimensions', 'cost', 'process', 'must_have', 'must_avoid', 'culture'].map(k => field(k)),
      field('deliverable', '概念效果图', ['概念效果图', '概念图与三维需求交接'])),
    output,
    el('label', { class: 'zs-check' }, demo, el('span', {}, '只是试做：这份设计里的意见不会用到以后同类设计的构思里')));
  for (const input of more.querySelectorAll('input[type=text], textarea')) input.placeholder = '不确定可以不填';
  const note = editing ? '更新要求后需重新选择方案，已有图片和版本都会保留。'
    : photo ? (imageReady ? null : '修改服务暂时不可用，这次只会上传照片，不会开始修改。')
    : !status.planner_ready ? '暂时无法出方案，可以先保存设计要求。'
    : !imageReady ? '出图服务暂时不可用，这次只出文字方案。' : null;
  const label = () => uploading ? (photo ? '正在上传…' : '正在创建…') : editing ? '保存修改'
    : photo?.mode === 'reference' ? '下一步：设置参考图片'
    : photo && !photo.mode ? '先选择图片用途'
    : photo ? '下一步：确认修改内容' : status.planner_ready ? startLabel(imageReady && !textOnly.checked, 0) : '建好设计';
  const clear = editing ? null : button('清空', () => {
    if (window.confirm('清空已填写的需求、设置和已选照片？未提交的内容将被丢弃。')) newTask();
  }, false, busy(), { class: 'zs-btn quiet' });
  const submit = el('button', { class: 'zs-btn primary', type: 'submit', disabled: busy() }, label());
  const photoHint = !editing && !photo && drafts.get('new:photo')?.[0]?.[1]
    ? el('p', { class: 'zs-help zs-photo-hint' }, `上次选的照片「${drafts.get('new:photo')[0][1]}」需要重新选一次。`) : null;
  const referenceHint = !editing && !photo && picked.size
    ? el('p', { class: 'zs-help zs-reference-hint' }, `已选 ${picked.size} 份参考素材，会用于构思。`,
      button('查看并调整', () => { rememberInputs(); go(1); }, false, busy(), { class: 'zs-btn quiet small' })) : null;
  const actions = el('div', { class: 'zs-form-actions' }, clear,
    !editing && !photo && cloudImages() ? button('用多张图组合', async () => {
      if (!form.reportValidity() || busy()) return;
      const values = briefValues(Object.fromEntries(new FormData(form))), epoch = navigation;
      uploading = true; render();
      try {
        const created = await createOnce(values);
        await loadTasks();
        if (epoch !== navigation || !panel?.open || !await chooseTask(created.id, { push: true })) return;
        finishCreation();
        drafts.set(created.id + ':3:compose', [['change', values.goal]]);
        beginComposition();
      } finally { uploading = false; if (panel?.open) render(); }
    }, false, busy(), { class: 'zs-btn quiet' }) : null,
    !editing && !photo && status.planner_ready ? button('先挑参考素材', () => { if (form.reportValidity()) { rememberInputs(); go(1); } }) : null,
    submit);
  const text = el('div', { class: 'zs-compose-text' }, goal, el('div', { class: 'zs-form-grid' }, title, category), more,
    el('div', { class: 'zs-form-footer' }, photoHint, referenceHint, note ? el('p', { class: 'zs-help' }, note) : null, actions));
  if (editing) {
    form.append(text);
    for (const [key, value] of Object.entries(task.brief)) { const control = form.elements.namedItem(key); if (control) control.value = value; }
  } else {
    form.append(photoPicker('new'), text);
    const update = () => {
      clear.hidden = !photo && !picked.size && ![...form.querySelectorAll('input[type=text], textarea')].some(n => n.value.trim());
      submit.textContent = label();
      submit.disabled = busy() || Boolean(photo && (!photo.mode || (photo.mode !== 'reference' && photo.files?.length > 1)));
    };
    form.dataset.sync = '1';
    form.addEventListener('input', update); form.addEventListener('change', update); update();
    form.addEventListener('paste', event => {
      const files = [...(event.clipboardData?.files || [])].filter(f => f.type.startsWith('image/'));
      if (!files.length) return;
      event.preventDefault();
      try { choosePhoto('new', files); } catch (error) { tell(error.message, true); }
    });
  }
  return form;
}
function taskOverview() {
  if (!task) return homeView();
  const facts = Object.entries(task.brief).filter(([k, v]) => !['title', 'goal'].includes(k) && v);
  return [heading(task.brief.title, task.brief.goal),
    facts.length ? el('dl', { class: 'zs-facts' }, facts.map(([k, v]) => el('div', {}, el('dt', {}, labels[k] || k), el('dd', {}, v)))) : null,
    task.is_demo ? el('p', { class: 'zs-help' }, '这是一份试做，里面的意见不会用到以后同类设计的构思里。') : null,
    el('details', { class: 'zs-details zs-edit-brief' }, el('summary', {}, '修改设计要求'), newForm(true))];
}
function homeView() {
  const search = el('input', { type: 'search', value: projectQuery, placeholder: '搜名称、材质或用途…', 'aria-label': '搜索设计', autocomplete: 'off',
    oninput: event => { projectQuery = event.target.value; renderProjectList(); } });
  return [el('section', { class: 'zs-home-new', 'aria-labelledby': 'zs-new-title' },
      el('div', { class: 'zs-heading' }, el('h2', { id: 'zs-new-title' }, '新建设计'),
        el('p', {}, '可以直接改一张图，也可以参考图片设计新方案；没有图片时，写下想法即可。')), newForm(), designTools()),
    el('section', { class: 'zs-home-list', 'aria-labelledby': 'zs-list-title' },
      el('div', { class: 'zs-section-head' }, el('h2', { id: 'zs-list-title' }, '我的设计'),
        el('label', { class: 'zs-search-field' }, icon('search'), search)),
      el('div', { class: 'zs-task-list' }), el('div', { class: 'zs-hidden-list' }))];
}
async function useStyle(pack, { replace = false } = {}) {
  if (busy()) throw Error('请等当前操作完成。');
  let image;
  if (!task) {
    if (pendingUploads.has('new')) throw Error('请先完成所选照片的上传，再从参考图片中加入风格。');
    const form = view.querySelector('.zs-home-new form');
    const values = briefValues(Object.fromEntries(new FormData(form)));
    values.goal ||= `设计一处${pack.use_case}，参考${pack.name}的材质搭配和氛围。${pack.description || ''}`;
    values.title ||= pack.name + '设计';
    if (!values.category || values.category === '其他设计') values.category = '空间与展陈';
    const epoch = navigation;
    const created = await createOnce({ ...values, workspace: 'reference_design' });
    await loadTasks();
    if (epoch !== navigation || !panel?.open || !await chooseTask(created.id, { push: true })) throw Error('设计已建好，请从我的设计中打开。');
    finishCreation();
    drafts.set(task.id + ':3:compose', [['change', values.goal]]);
    beginComposition(true);
    image = { id: 'compose' };
  } else if (compositionTask === task.id) image = { id: 'compose' };
  else if (!task.images.length) {
    beginComposition(true); image = { id: 'compose' };
  } else image = task.images.find(row => row.id === activeImageId) || task.images.at(-1);
  const draft = currentEdit(image);
  const curated = new Set(stylePacks.flatMap(pack => pack.references.map(ref => ref.id)));
  const incomingIds = new Set(pack.references.map(ref => ref.id));
  const kept = draft.rows.filter(row => !(replace && row.source === 'reference' && row.use === 'style' && curated.has(row.id) && !incomingIds.has(row.id)));
  const additions = pack.references.filter(ref => !kept.some(row => row.source === 'reference' && row.id === ref.id));
  const room = maxEditRows;
  if (kept.length + additions.length > room) throw Error('参考图已接近上限，请移除几张后再选用。');
  const selected = styleReferences(task.references || [], draft.rows, pack.references, curated, replace, draft.suppressed);
  if (selected.length > 12) throw Error('已保存的参考素材过多，请先移除不需要的素材。');
  const nextRows = styleEditRows(draft.rows, pack.references, curated, selected, replace, base + 'reference-image/');
  const interior = image.id === 'compose' ? task.interior_input : null;
  if (interior && !status.interior_style_binding) throw Error('当前服务尚不支持同步更换室内风格，未改动所选参考。请重新连接后核对。');
  const options = interior ? { references: selected, edits: nextRows, input: { ...interior,
    ...(replace ? { style_label: pack.name, style_description: pack.description || '' } : {}) } } : { references: selected };
  if (!await action(interior ? 'interior-input' : 'references', options))
    throw Error('页面已切换，请重新打开这份设计。');
  draft.rows = nextRows; draft.referencesTouched = true;
  rememberPicks(); saveEditDraft(image); render();
  tell(interior ? (replace ? `已换为${pack.name}，下一次生成时使用。` : '已添加风格参考，主风格不变。') + '设计说明和已有图片都保留。'
    : task.interior_input ? `已为这张图加入${pack.name}参考，接下来修改这张图时使用。按平面图生成新方案仍使用主风格${task.interior_input.style_label}。`
    : (replace ? '已更新风格，同一素材的取用说明和选区已保留。' : !additions.length ? '所选素材已在本次参考中，保留原用途。' : '已添加风格参考。')
      + '原图和结构底稿不变。确认参考用途和设计内容后再生成。');
}
function browseStyles() {
  const current = task && (compositionTask === task.id || !task.images.length ? { id: 'compose' }
    : task.images.find(row => row.id === activeImageId) || task.images.at(-1));
  const selectedIds = current ? currentEdit(current).rows.filter(row => row.source === 'reference' && row.use === 'style').map(row => row.id) : [];
  return openStyles({ packs: stylePacks, onUse: useStyle, disabled: busy(), selectedIds,
    interior: Boolean(current?.id === 'compose' && task.interior_input) });
}
function changeInteriorStyle() {
  if (busy() || !task?.interior_input) return;
  const owner = task.id, epoch = navigation;
  return openInteriorStyle({ packs: stylePacks, current: task.interior_input, apply: async (pack, options) => {
    if (!currentTask(owner, epoch)) throw Error('页面已切换，请在当前设计中更换风格。');
    if (busy()) throw Error('请等当前操作完成。');
    beginComposition(true);
    const editEpoch = navigation;
    await useStyle(pack, options);
    if (!options.generate) return;
    if (!currentTask(owner, editEpoch)) throw Error('风格已保存，请在这份设计中继续生成。');
    let budget = referenceBudgetState({ id: 'compose' });
    while (budget.state === 'loading' && budget.ready) {
      await budget.ready;
      if (!currentTask(owner, editEpoch)) throw Error('风格已保存，请在这份设计中继续生成。');
      budget = referenceBudgetState({ id: 'compose' });
    }
    const text = view.querySelector('[name="change"]')?.value ?? task.brief.goal;
    if (await generateComposition({ id: 'compose' }, text))
      tell('新风格的室内效果正在生成，之前的图片仍可查看。');
  } });
}
function designTools() {
  return el('section', { class: 'zt-tools', 'aria-label': '设计工具' },
    el('div', { class: 'zt-tool-copy' }, el('strong', {}, '精选风格'), el('p', {}, `${stylePacks.length} 个方向 · 实景、馆藏与原创概念`),
      button('浏览并选用', browseStyles, false, busy(), { class: 'zs-btn quiet small' })),
    el('div', { class: 'zt-tool-previews', 'aria-hidden': 'true' }, stylePacks.slice(0, 2).map(pack => pack.references[0]
      ? el('img', { src: base + 'reference-image/' + pack.references[0].id, alt: '', loading: 'lazy' }) : null)),
    el('div', { class: 'zt-tool-copy zt-floorplan-entry' }, el('strong', {}, '室内实景'), el('p', {}, '上传平面图，选择风格，可补充参考照片'),
      button('平面图生成室内实景', () => openInterior({ packs: stylePacks, prepare: prepareInterior, validateFile: validateUpload }), false,
        busy() || !status.photographic_interior || !cloudImages(), { class: 'zs-btn quiet small' }),
      el('details', { class: 'zs-details' }, el('summary', {}, '高级结构工具'),
        el('p', { class: 'zs-help' }, '矩形结构研究保留原有底稿流程，不作为室内实景入口。'),
        button('打开矩形结构试例', () => openFloorplan({ packs: stylePacks, prepare: prepareFloorplan, continueSaved: continueFloorplan, completed: finishCreation }), false, busy(), { class: 'zs-btn quiet small' }))));
}
async function prepareInterior({ plan, extras, style, goal, startGeneration = true, reportProgress = () => {} }) {
  if (busy()) throw Error('请等当前操作完成。');
  if (!cloudImages() || !status.photographic_interior) throw Error('室内实景服务暂不可用，未提交生成。');
  if (!globalThis.crypto?.subtle) throw Error('当前地址无法校验原图。请通过本机 localhost 或 HTTPS 打开工作台。');
  const files = [plan, ...extras.map(item => item.file)]; files.forEach(validateUpload);
  const selected = style.references.map(row => ({ id: row.id, role: '风格', use: 'style',
    take: row.style_take || '只参考配色、材质、装饰层次和照明，不照搬场景布局。' }));
  if (files.length + selected.length > 10) throw Error('平面图、风格和补充参考合计最多10张。');
  const hashes = await Promise.all(files.map(async file => [...new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()))]
    .map(byte => byte.toString(16).padStart(2, '0')).join('')));
  if (hashes.slice(1).includes(hashes[0])) throw Error('原始平面图已单独选用，请不要把同一张图再次作为补充参考。');
  const epoch = navigation;
  const description = `依据上传的原始平面图，设计整套照片级室内效果，分别呈现各个空间的人眼高度广角视角。${goal ? '补充要求：' + goal : ''}`;
  const brief = { title: '室内实景 · ' + plan.name.replace(/\.[^.]+$/, '').slice(0, 80), goal: description, category: '空间与展陈', workspace: 'reference_design',
    must_have: '以平面图可辨认的墙体、门窗和家具位置为布局依据。',
    must_avoid: '灰模、鸟瞰剖切图、线稿、塑料质感、尺寸文字和多图拼贴。', deliverable: '室内摄影效果图' };
  uploading = true; render();
  let created;
  try {
    created = await createOnce(brief, interiorPreparationIdentity(style, extras, hashes));
    if (created.floorplan || created.images.length || created.concepts.length || created.job?.status === 'running')
      throw Error('这份设计已进入后续操作，未重新准备或覆盖。请从我的设计中打开它核对。');
    const assets = [];
    for (const [index, file] of files.entries()) {
      reportProgress(`正在保存第 ${index + 1} / ${files.length} 张图片：${file.name}`);
      const find = value => value.edit_assets?.find(row => row.source_sha256 === hashes[index] && row.upload_mode === 'reference' && !row.guide_kind);
      let asset = find(created);
      if (!asset) {
        let uploaded;
        try { uploaded = await uploadImage(created.id, created.revision, { file, mode: 'reference' }); }
        catch (error) {
          const fresh = await api('tasks/' + created.id).catch(() => null);
          if (!fresh || !error.uploadUncertain || !find(fresh)) throw error;
          uploaded = fresh;
        }
        created = uploaded; asset = find(created);
      }
      if (!asset) throw Error('未能确认已保存的图片：' + file.name + '。未提交生图，请重试准备。');
      assets.push(asset);
    }
    const referencesMatch = value => value.references.length === selected.length
      && selected.every(ref => value.references.some(row => row.id === ref.id && row.role === ref.role && row.use === ref.use && row.take === ref.take));
    if (!referencesMatch(created)) {
      if (created.references.length) throw Error('已保存的参考设置被改动，未覆盖。请打开这份设计核对。');
      try { created = await api('tasks/' + created.id + '/references', { revision: created.revision, references: selected }); }
      catch (error) {
        if (!error.submissionUncertain) throw error;
        const fresh = await api('tasks/' + created.id);
        if (!referencesMatch(fresh)) throw error;
        created = fresh;
      }
    }
    const proof = await api('asset-proof/' + created.id + '/' + assets[0].id);
    reportProgress('正在核对原始平面图，并保存风格和参考用途…');
    const input = { schema: 1, source_id: assets[0].id, source_sha256: proof.sha256, style_label: style.name,
      style_description: style.description || '' };
    const edits = [{ source: 'upload', id: assets[0].id, use: 'reference', fidelity: 'reference', take: '只读取完整空间布局、墙体、门窗与家具位置。' },
      ...selected.map(ref => ({ source: 'reference', id: ref.id, use: ref.use, take: ref.take, fidelity: 'reference' })),
      ...extras.map((item, index) => interiorSupplementReference(item, assets[index + 1].id))];
    const matches = value => Object.entries(input).every(([key, expected]) => value.interior_input?.[key] === expected)
      && value.interior_input.edits?.length === edits.length
      && edits.every((row, index) => Object.entries(row).every(([key, expected]) => value.interior_input.edits[index]?.[key] === expected)
        && !value.interior_input.edits[index]?.source_selection);
    if (!matches(created)) {
      if (created.interior_input) throw Error('平面图或风格绑定已变化，未覆盖。请打开已保存的设计核对。');
      try { created = await api('tasks/' + created.id + '/interior-input', { revision: created.revision, input, edits }); }
      catch (error) {
        if (!error.submissionUncertain) throw error;
        const fresh = await api('tasks/' + created.id);
        if (!matches(fresh)) throw error;
        created = fresh;
      }
    }
    if (!matches(created)) throw Error('返回的平面图或风格记录与本次选择不同，未进入生成。');
    await loadTasks();
    if (epoch !== navigation || !panel?.open || !await chooseTask(created.id, { push: true })) throw Error('资料已保存。请从我的设计中打开这份室内设计继续。');
    const draft = currentEdit({ id: 'compose' }); draft.designMode = true; draft.count = 1;
    drafts.set(created.id + ':3:compose', [['change', description], ['aspect', '4:3'], ['size', '2K']]);
    draftKey = null; saveDrafts(); beginComposition(true); finishCreation();
    if (startGeneration) {
      reportProgress('资料已保存，正在开始生成室内效果…');
      try {
        const accepted = await action('design', { change: description, edits, count: 1, quality: '精细', size: '2K', aspect: '4:3' });
        if (accepted) tell('室内效果正在生成，完成后会显示在这份设计里。');
      } catch (error) { tell(error.message, true); }
    } else tell('平面图和风格已保存，可以调整要求后再生成。');
  } catch (error) {
    if (created) await loadTasks().catch(() => {});
    throw error;
  } finally { uploading = false; if (panel?.open) render(); }
}
function interiorBasis(record = task?.interior_input) {
  if (!record) return null;
  return el('details', { class: 'zs-details zf-basis', 'data-disclosure': 'interior-basis' },
    el('summary', {}, '室内设计依据', el('small', {}, ' ' + record.style_label)),
    el('a', { href: base + 'asset/' + task.id + '/' + record.source_id, target: '_blank', rel: 'noopener' },
      el('img', { src: base + 'asset/' + task.id + '/' + record.source_id, alt: '本轮使用的原始平面图', width: 240, loading: 'lazy' })),
    record.style_description ? el('p', { class: 'zs-help' }, record.style_description) : null,
    el('p', { class: 'zs-help' }, '原始平面图提供布局，风格与补充图片按各自用途选用。结果需核对墙体、门窗、家具位置和材质；效果图不能替代施工图或尺寸测量。'));
}
async function openPreparedFloorplan(receipt, styleId, goal) {
  if (!floorplanReceiptValid(receipt, receipt?.source_sha256)) throw Error('续接记录无效，请从我的设计中打开试例。');
  if (!panel?.open) throw Error('请重新打开工作台后继续。');
  const epoch = navigation;
  const style = styleId ? stylePacks.find(pack => pack.id === styleId && pack.available) : null;
  let stylePending = styleId && !style ? '原风格暂不可用，尚未选用。请在试例中核对后另选风格。' : '';
  let styleApplied = false;
  if (epoch !== navigation || !await chooseTask(receipt.task_id, { push: true })) throw Error('页面已切换，请重新打开这份试例。');
  const ownerEpoch = navigation;
  if (!floorplanMatchesReceipt(task, receipt)) throw Error('这份试例的结构已变化，未套用旧记录。请直接打开并核对当前底稿。');
  const sources = [{ id: receipt.source_id, sha256: receipt.stored_source_sha256 }, ...receipt.guides];
  const proofs = await Promise.all(sources.map(row => api('asset-proof/' + receipt.task_id + '/' + row.id)));
  if (proofs.some((proof, index) => proof.sha256 !== sources[index].sha256))
    throw Error('已保存源图或底稿的内容与记录不同。未重新准备或覆盖，请直接打开试例核对。');
  if (!currentTask(receipt.task_id, ownerEpoch)) throw Error('页面已切换，请重新打开这份试例。');
  if (style) {
    const selected = style.references.map(row => ({ id: row.id, role: '风格' }));
    const matches = value => value.references?.length === selected.length
      && selected.every(ref => value.references.some(row => row.id === ref.id && row.role === ref.role));
    if (matches(task)) styleApplied = true;
    else {
      if (task.references.length) stylePending = '参考资料已变化，原风格尚未选用；当前用途和取用说明未覆盖。请在试例中核对后选用风格。';
      else if (task.revision !== receipt.revision) stylePending = '试例版本已更新，原风格尚未选用。请在试例中核对当前设置。';
      else {
        let next;
        try { next = await api('tasks/' + task.id + '/references', { revision: task.revision, references: selected }); }
        catch (error) {
          if (!error.submissionUncertain) throw error;
          const checked = await api('tasks/' + receipt.task_id);
          if (!matches(checked)) throw error;
          next = checked;
        }
        if (!currentTask(receipt.task_id, ownerEpoch)) throw Error('页面已切换，请重新打开这份试例。');
        if (!floorplanMatchesReceipt(next, receipt)) throw Error('底稿已变化，未套用旧记录。请直接打开试例核对。');
        task = next; picked = new Map(task.references.map(ref => [ref.id, ref.role])); rememberPicks(); styleApplied = true;
      }
    }
  }
  const image = { id: 'compose' }, draft = currentEdit(image);
  if (styleApplied) {
    const additions = style.references.filter(ref => !draft.rows.some(row => row.source === 'reference' && row.id === ref.id));
    if (additions.some(ref => draft.suppressed?.has?.(ref.id)))
      stylePending = '风格已保存在设计记录，但本次草稿已移除其中的参考。请在试例中核对后再选用。';
    else if (draft.rows.length + additions.length > maxEditRows)
      stylePending = '风格已保存在设计记录，但参考卡片已满，尚未加入本次参考。请先移除不用的卡片，再选用风格。';
    else for (const ref of additions) {
      const stored = task.references.find(row => row.id === ref.id);
      draft.rows.push({ source: 'reference', id: ref.id, name: ref.title,
        preview: base + 'reference-image/' + ref.id, use: stored?.use || referenceUse(stored?.role || '风格'), fidelity: 'reference',
        take: stored?.take ?? '只参考材质搭配、装饰层次和光线，不照搬人物或宗教细节。',
        ...(stored?.source_selection ? { source_selection: stored.source_selection, source_sha256: stored.source_sha256 } : {}) });
    }
  }
  draft.designMode = true; saveEditDraft(image);
  const key = task.id + ':3:compose';
  if (!drafts.has(key)) drafts.set(key, [['change', goal || task.brief.goal], ['aspect', '4:3'], ['size', '2K']]);
  tasks = [task, ...tasks.filter(row => row.id !== task.id)];
  draftKey = null; saveDrafts(); beginComposition(true);
  tell('原图和三份结构底稿已保存。' + (stylePending || '先核对布置与参考设置，再确认生成效果图。'), Boolean(stylePending));
  return { opened: true, receipt, stylePending };
}
async function continueFloorplan({ receipt, styleId }) {
  if (busy()) throw Error('请等当前操作完成后再继续。');
  uploading = true; render();
  try { return await openPreparedFloorplan(receipt, styleId); }
  finally { uploading = false; if (panel?.open) render(); }
}
async function prepareFloorplan({ spec, file, style, sourceKind = 'example', sourceSha256, onSaved }) {
  if (busy()) throw Error('请等当前操作完成。');
  const viewIssue = floorplanViewIssue(spec);
  if (viewIssue) throw Error(viewIssue);
  if (sourceKind === 'upload' && !file) throw Error('自选源图不可用，请重新选择，已填写内容不会改用试例图。');
  if (!globalThis.crypto?.subtle) throw Error('当前地址无法校验原图。请通过本机 localhost 或 HTTPS 打开工作台。');
  const epoch = navigation;
  if (!file) {
    const response = await fetch(base + 'floorplan-example');
    if (!response.ok) throw Error('原始试例图暂不可用。');
    file = new File([await response.blob()], '室内平面尺寸图.jpg', { type: 'image/jpeg' });
  }
  validateUpload(file);
  const sourceDigest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()))].map(v => v.toString(16).padStart(2, '0')).join('');
  if (sourceDigest !== sourceSha256) throw Error('源图内容已变化或尚未核对。未创建试例，请重新打开并核对源图。');
  if (epoch !== navigation) throw Error('页面已切换，请重新打开试做。');
  const height = spec.height_kind === 'assumed' ? '试做假设，待确认' : '已知值，依据已记录';
  const goal = `根据已确认的矩形平面图制作空间试例，保留墙体与整面开口关系。${style ? '材质与光线参考' + style.name + '。' : '材质与风格以本次参考和设计说明为准。'}`;
  const brief = { goal, title: (sourceKind === 'upload' ? '自选平面图' : spec.layout === 'booth' ? '春季佛事展' : '矩形空间') + ' · ' + spec.view + '视角试例', category: '空间与展陈',
    dimensions: `净宽${spec.width_mm} mm × 净长${spec.length_mm} mm；层高${spec.height_mm} mm（${height}）`,
    must_have: '以已确认的平面结构为准，不新增门窗，不封闭整面开口。', culture: '纹样和宗教细节须专业复核。',
    is_demo: true, workspace: 'reference_design' };
  uploading = true; render();
  let created;
  try {
    created = await createOnce(brief);
    const oldSource = created.edit_assets?.find(row => row.source_sha256 === sourceDigest && row.upload_mode === 'reference' && !row.guide_kind);
    const uploaded = oldSource ? created : await uploadImage(created.id, created.revision, { file, mode: 'reference' });
    const asset = uploaded.edit_assets.find(row => row.source_sha256 === sourceDigest && row.upload_mode === 'reference');
    if (!asset) throw Error('未能定位原始平面图，请重新上传。');
    const proof = await api('asset-proof/' + uploaded.id + '/' + asset.id);
    const matches = value => value.floorplan?.source_id === asset.id && value.floorplan.source_sha256 === proof.sha256
      && floorplanSpecMatch(value.floorplan.spec, spec) && Boolean(floorplanReceipt(value, sourceDigest));
    let prepared = uploaded;
    if (!matches(prepared)) {
      try { prepared = await api('tasks/' + uploaded.id + '/floorplan', { revision: uploaded.revision, source_id: asset.id, source_sha256: proof.sha256, spec }); }
      catch (error) {
        if (!error.submissionUncertain) throw error;
        const checked = await api('tasks/' + uploaded.id);
        if (!matches(checked)) throw error;
        prepared = checked;
      }
    }
    if (!matches(prepared)) throw Error('服务返回的底稿与本次源图或结构不同。未套用，请打开试例核对。');
    const receipt = floorplanReceipt(prepared, sourceDigest);
    const recorded = onSaved?.(receipt);
    if (recorded?.ok !== false) finishCreation();
    try {
      if (epoch !== navigation) throw Error('页面已切换，请重新打开这份试例。');
      return await openPreparedFloorplan(receipt, style?.id, goal);
    } catch (error) { return { opened: false, receipt, message: error.message }; }
  } finally { uploading = false; if (panel?.open) render(); }
}
function floorplanBasis(record = task?.floorplan) {
  if (!record) return null;
  const spec = record.spec;
  return el('details', { class: 'zs-details zf-basis', 'data-disclosure': 'floorplan-basis' },
    el('summary', {}, '平面结构与尺寸依据', el('small', {}, ` ${spec.width_mm} × ${spec.length_mm} mm · ${spec.view}视角`)),
    el('p', { class: 'zs-help' }, `净宽：${spec.width_mm_source}；净长：${spec.length_mm_source}。层高 ${spec.height_mm} mm，${spec.height_kind === 'assumed' ? '试做假设，待确认' : '已知值'}：${spec.height_mm_source}。`),
    el('p', { class: 'zs-help' }, spec.layout_source),
    floorplanViewIssue(spec) ? el('p', { class: 'zd-notice', role: 'status', 'data-error': 'true' }, floorplanViewIssue(spec)
      + ' 请返回新建设计，重新准备所需视角；已有底稿和历史图片仍可查看、下载。') : null,
    el('div', { class: 'zf-guide-grid' }, [
      { id: record.source_id, name: '原始平面图' }, ...(record.guides || task.edit_assets?.filter(row => record.guide_ids?.includes(row.id)) || [])
    ].map(row => el('a', { href: base + 'asset/' + task.id + '/' + row.id, target: '_blank', rel: 'noopener' },
      el('img', { src: base + 'asset/' + task.id + '/' + row.id, alt: row.name, loading: 'lazy' }), el('span', {}, row.name)))),
    el('p', { class: 'zs-help' }, '底稿通过确定的几何参数绘制；当前出图引擎以图片作参考，仍可能改变细节。请核对墙体、开口、功能区和视角，不能据效果图量尺寸。'));
}
function resetReferences() {
  ++refsRequest;
  refs = []; refsSearched = false; refsLoading = false; refsError = ''; refsQuery = ''; refsSelectedOnly = false;
}
async function loadReferences() {
  const owner = task?.id || 'new', epoch = navigation, request = ++refsRequest;
  refsLoading = true; refsError = ''; refsSearched = true; renderRefs();
  const current = () => request === refsRequest && panel?.open && navigation === epoch && (task?.id || 'new') === owner;
  try {
    const query = refsSelectedOnly ? 'selected=' + [...picked.keys()].join(',') : 'q=' + encodeURIComponent(refsQuery);
    const result = refsSelectedOnly && !picked.size ? { references: [] } : await api('references?' + query);
    if (current()) refs = result.references;
  } catch (error) {
    if (current()) { refs = []; refsError = error.message; }
  } finally {
    if (current()) { refsLoading = false; renderRefs(); }
  }
}
async function search(query) { refsQuery = query; refsSelectedOnly = false; await loadReferences(); }
function renderRefs() {
  const grid = view.querySelector('.zs-reference-grid'); if (!grid) return;
  const browsing = stage === 5;
  for (const ref of refs) refInfo.set(ref.id, { kind: ref.kind, source_id: ref.source_id, title: ref.title });
  grid.setAttribute('aria-busy', String(refsLoading));
  grid.replaceChildren();
  for (const ref of refsLoading || refsError ? [] : refs.filter(ref => !refsSelectedOnly || picked.has(ref.id))) {
    const active = !browsing && picked.has(ref.id);
    const role = el('select', { 'aria-label': '参考用途：' + ref.title, disabled: busy() }, status.roles.map(r => el('option', { value: r }, '参考' + r)));
    role.value = picked.get(ref.id) || '形态';
    role.addEventListener('change', () => { if (picked.has(ref.id)) { picked.set(ref.id, role.value); rememberPicks(); } });
    const pick = button(active ? '已选用' : '选用', () => {
      if (picked.has(ref.id)) picked.delete(ref.id);
      else { if (picked.size >= 12) throw Error('最多选 12 份素材。'); picked.set(ref.id, role.value); }
      rememberPicks();
      renderRefs();
      (grid.querySelector(`[data-ref="${ref.id}"] .zs-pick`) || view.querySelector('[data-reference-filter]'))?.focus({ preventScroll: true });
    }, active, busy(), { class: 'zs-btn small zs-pick' + (active ? ' primary' : ''), 'aria-pressed': String(active) });
    grid.append(el('article', { class: 'zs-ref-card' + (active ? ' chosen' : ''), 'data-ref': ref.id },
      ref.kind === 'image' ? el('img', { src: base + 'reference-image/' + ref.id, loading: 'lazy', alt: ref.title, width: '320', height: '240' })
        : el('div', { class: 'zs-doc' }, '文字资料'),
      el('div', { class: 'zs-ref-body' }, el('h4', {}, ref.title),
        el('p', { class: 'zs-meta' }, [ref.origin, ref.rights].filter(Boolean).join('，')),
        el('p', { class: 'zs-ref-text' }, ref.fields.visual_description?.slice(0, 160) || '还没有图像描述，可以先打开原笔记看看。'),
        el('a', { href: noteURL(ref.note), class: 'zs-link' }, '打开原笔记'),
        browsing ? null : el('div', { class: 'zs-row' }, role, pick))));
  }
  if (refsLoading) grid.append(el('p', { class: 'zs-help', role: 'status' }, '正在加载参考素材…'));
  else if (refsError) grid.append(empty('参考素材暂时没加载出来', refsError, button('重新加载', loadReferences, false, false)));
  else if (!grid.children.length && refsSearched) grid.append(refsSelectedOnly
    ? empty('还没有选参考素材', '回到素材列表挑几份想参考的。', button('返回素材列表', () => search(refsQuery), false, false))
    : empty('没有找到相关素材', '换个说法试试，比如作品名称、纹样或材质：“莲花”“石材”。'));
  const filter = view.querySelector('[data-reference-filter]');
  if (filter) { filter.textContent = refsSelectedOnly ? '返回素材列表' : '只看已选'; filter.setAttribute('aria-pressed', String(refsSelectedOnly)); }
  renderPickedReferences();
  updatePickBar();
}
function removePickedReference(id) {
  if (busy() || !picked.delete(id)) return;
  if (refsSelectedOnly && !picked.size) {
    ++refsRequest; refs = []; refsLoading = false; refsError = '';
  }
  rememberPicks(); renderRefs();
  (view.querySelector('[data-picked-list] button') || view.querySelector('[data-reference-filter]'))?.focus({ preventScroll: true });
}
function renderPickedReferences() {
  const list = view.querySelector('[data-picked-list]'); if (!list) return;
  list.hidden = !picked.size;
  list.replaceChildren(...[...picked].map(([id, role]) => {
    const title = refInfo.get(id)?.title || task?.references.find(ref => ref.id === id)?.title ||
      stylePacks.flatMap(pack => pack.references).find(ref => ref.id === id)?.title || '素材 ' + id.slice(0, 8);
    return el('div', { class: 'zs-picked-reference', role: 'listitem' },
      el('span', { class: 'zs-picked-reference-label' }, title, el('small', {}, '参考' + role)),
      button('移除', () => removePickedReference(id), false, busy(),
        { class: 'zs-btn quiet small', 'aria-label': '移除参考素材：' + title }));
  }));
}
function rememberPicks() {
  drafts.set((task?.id || 'new') + ':refs', [['picked', [...picked]]]);
  saveDrafts();
}
function imageBudget(ids, info, limit) {
  const sources = new Set(), unknown = [];
  for (const id of ids) {
    const row = info.get(id);
    if (!row) unknown.push(id);
    else if (row.kind === 'image') sources.add(row.source_id || id);
  }
  return { images: sources.size, over: limit ? Math.max(0, sources.size - limit) : 0, unknown };
}
function budgetText(budget, limit) {
  return `所选素材里有 ${budget.images} 张图片，出图一次最多参考 ${limit} 张（文字资料不算）。请减掉 ${budget.over} 张图片，或这次只出文字方案。`;
}
function imageLimit() { return cloudImages() ? status.image_max_references || 0 : 0; }
async function checkImageBudget(render) {
  const limit = imageLimit(), ids = [...picked.keys()];
  if (!render || !limit || !ids.length) return;
  const { unknown } = imageBudget(ids, refInfo, limit);
  if (unknown.length) for (const ref of (await api('references?selected=' + unknown.join(','))).references)
    refInfo.set(ref.id, { kind: ref.kind, source_id: ref.source_id });
  const budget = imageBudget(ids, refInfo, limit);
  if (budget.over) throw Error(budgetText(budget, limit));
}
async function launchPlan(options, values) {
  const owner = task?.id, epoch = navigation;
  let problem;
  try { await checkImageBudget(options.render); } catch (error) { problem = error; }
  if (epoch !== navigation || task?.id !== owner) return;
  if (problem) { renderRefs(); throw problem; }
  return task ? startPlan(picked.size > 0, options) : createDesign(briefValues(values), null, options, epoch, [...picked]);
}
function updatePickBar() {
  const count = view.querySelector('[data-reference-count]'), direct = view.querySelector('[data-direct-plan]');
  if (!count || !direct) return;
  const render = direct.dataset.render === '1', limit = imageLimit();
  const budget = imageBudget([...picked.keys()], refInfo, limit), over = render && budget.over > 0;
  count.textContent = over ? budgetText(budget, limit) : picked.size ? `已选 ${picked.size} 份，最多 12 份` : '还没有选，最多 12 份';
  count.dataset.state = over ? 'warn' : '';
  const textPlan = view.querySelector('[data-text-plan]');
  if (textPlan) textPlan.hidden = !over;
  if (!busy()) { direct.textContent = startLabel(render, picked.size); direct.disabled = over || !status.planner_ready; }
}
function referenceSearch() {
  const input = el('input', { type: 'search', value: refsQuery, placeholder: '搜作品名、纹样或材质，例如：莲花 石材…', 'aria-label': '搜索参考资料', autocomplete: 'off',
    oninput: event => { refsQuery = event.target.value; } });
  return el('form', { class: 'zs-search', role: 'search', onsubmit: async e => { e.preventDefault(); try { await search(input.value); } catch (err) { tell(err.message, true); } } },
    el('label', { class: 'zs-search-field' }, icon('search'), input), el('button', { class: 'zs-btn', type: 'submit' }, '搜索'));
}
function referenceView() {
  const values = task ? null : Object.fromEntries(drafts.get('new:0') || []);
  if (!task && !values.goal?.trim()) return [empty('先写下想做什么', '回到新建设计，写好需求再来挑参考素材。', button('去写需求', () => go(0), true, false))];
  const searchForm = referenceSearch();
  const options = el('form', { class: 'zs-plan-options', 'data-sync': '1', onsubmit: event => event.preventDefault() });
  const textOnly = el('input', { type: 'checkbox', name: 'text_only', value: '1', disabled: busy(), 'data-sync': '1' });
  if (task && status.image_ready !== false) {
    const { box, summary } = settings();
    options.append(el('details', { class: 'zs-output-options' }, el('summary', {}, '图片尺寸和比例', summary), box),
      el('label', { class: 'zs-check' }, textOnly, el('span', {}, '先只出文字方案，图片以后再出')));
  }
  const plan = () => planOptions(task ? Object.fromEntries(new FormData(options)) : values);
  const direct = button(planning ? '正在出方案…' : uploading ? '正在创建…' : startLabel(Boolean(plan().render), picked.size),
    () => launchPlan(plan(), values), true, busy() || !status.planner_ready);
  direct.dataset.directPlan = '1';
  direct.dataset.render = plan().render ? '1' : '0';
  const textPlan = button('这次只出文字方案', () => launchPlan({}, values), false, busy() || !status.planner_ready,
    { class: 'zs-btn quiet', 'data-text-plan': '1', hidden: true });
  options.addEventListener('change', () => {
    direct.dataset.render = plan().render ? '1' : '0';
    updatePickBar();
  });
  const brief = task ? task.brief : { title: values.title?.trim() || '新设计', goal: values.goal };
  return [!task ? heading('挑参考素材', '从收集的参考图和客户提供的资料里挑几份，出方案时会参考它们。也可以一份都不挑。')
      : task.concepts.length ? heading('重新构思', '会再出三个方案。之前的方案和图片都保留，但现在选中的方案会取消选择，需要重新挑。')
      : heading('出三个方案', '可以先挑几份参考素材，出方案时会参考它们；也可以直接开始。'),
    el('div', { class: 'zs-plan-bar' }, el('div', {}, el('strong', {}, brief.title), el('p', {}, brief.goal)),
      task ? null : button('改需求', () => go(0), false, false, { class: 'zs-btn quiet small' })),
    !status.planner_ready ? el('p', { class: 'zs-help' }, '暂时无法出方案，可以先上传照片直接修改。') : null,
    options.childElementCount ? options : null,
    task ? uploadPanel('有现场照片？', '上传后直接在照片上改，不用出方案。') : null,
    el('section', { class: 'zs-refs', 'aria-labelledby': 'zs-refs-title' },
      el('div', { class: 'zs-section-head' }, el('h3', { id: 'zs-refs-title' }, '参考素材', el('span', { class: 'zs-optional' }, '可选')),
        button('只看已选', async () => {
          if (!refsSelectedOnly && !picked.size) throw Error('还没有选参考素材。');
          refsSelectedOnly = !refsSelectedOnly;
          await loadReferences();
        }, false, false, { class: 'zs-btn quiet small', 'data-reference-filter': '1' })),
      el('div', { class: 'zs-picked-list', 'data-picked-list': '1', role: 'list', 'aria-label': '已选参考素材' }),
      searchForm, el('div', { class: 'zs-reference-grid' })),
    el('div', { class: 'zs-pick-bar' }, el('span', { class: 'zs-help', 'data-reference-count': '1' }), el('div', { class: 'zs-pick-actions' }, textPlan, direct))];
}
function valueIn(root, name, fallback = '') {
  const nodes = [...root.querySelectorAll(`[name="${name}"]`)];
  return (nodes[0]?.type === 'radio' ? nodes.find(n => n.checked) : nodes[0])?.value || fallback;
}
function settings(aspect = '1:1', size = null, structureAspect = null) {
  const cloud = cloudImages();
  const sizes = status.image_sizes?.length ? status.image_sizes : ['1K', '2K'];
  const chosen = sizes.includes(size) ? size : sizes.includes('2K') ? '2K' : sizes[0];
  const ratios = [['1:1', '方形 1:1'], ['4:3', '横向 4:3'], ['3:4', '竖向 3:4']];
  if (cloud && aspect === 'source') ratios.unshift(['source', '原图比例']);
  const structureNotice = structureAspect ? el('p', { class: 'zs-help', role: 'status', 'aria-live': 'polite', hidden: true }) : null;
  const box = el('div', { class: 'zs-settings', 'data-sync': '1' },
    cloud ? null : segmented('quality', '精细', [['精细', '精细'], ['快速', '快速']], '生成档位'),
    segmented('size', chosen, sizes.map(s => [s, s]), '尺寸'),
    segmented('aspect', ratios.some(([value]) => value === aspect) ? aspect : '1:1', ratios, '比例'),
    el('p', { class: 'zs-help', 'data-native-size': '1' }), structureNotice);
  const summary = el('span', { class: 'zs-settings-value' });
  const update = () => {
    const s = valueIn(box, 'size', chosen), ratio = valueIn(box, 'aspect', '1:1'), pixels = status.image_dimensions?.[s]?.[ratio];
    box.querySelector('[data-native-size]').textContent = ratio === 'source' ? '按底图宽高比出图，实际尺寸显示在新图下方。' : pixels ? `出图 ${pixels[0]} × ${pixels[1]} 像素` : '';
    if (structureNotice) {
      structureNotice.hidden = ratio === structureAspect;
      structureNotice.textContent = ratio === structureAspect ? '' : `结构底稿为 ${structureAspect}。其他比例可能改变构图和空间关系；建议按底稿比例出图，结果仍需逐项核对。`;
    }
    summary.textContent = `${s}，${ratio === 'source' ? '原图比例' : ratio}`;
  };
  box.addEventListener('change', update); update();
  return { box, summary };
}
function blocker() {
  if (status.image_ready === false) return el('p', { class: 'zs-help zs-blocker' }, '生成服务暂时不可用，请联系管理员。现有图片仍可以查看和下载。');
  if (tasks.some(t => t.id !== task?.id && t.job?.status === 'running')) return el('p', { class: 'zs-help zs-blocker' }, '另一份设计正在生成，它完成后才能开始新的生成。');
  return null;
}
function getSettings() {
  return { quality: valueOf('quality', '精细'), size: valueOf('size', '2K'), aspect: valueOf('aspect', '1:1') };
}
function quiet(node) { node.classList.remove('primary'); return node; }
function latest(conceptId) { return task.images.filter(i => i.concept_id === conceptId).at(-1); }
function continueConcept(id, remember = true) {
  endComposition();
  const previous = task.images.find(image => image.id === imageSelection.get(task.id));
  const image = remember && previous?.concept_id === id ? previous : latest(id);
  if (image) imageSelection.set(task.id, image.id);
  else {
    imageSelection.delete(task.id);
    try { sessionStorage.removeItem('zyb.studio.base.' + task.id); } catch {}
  }
  galleryOpen = false; comparing = false; changesOpen = false; go(3);
}
function chooseConcept(id) {
  return async () => { if (await action('select', { concept_id: id })) {
    continueConcept(id, false);
  } };
}
function cardState(conceptId) {
  if ((task.concepts || []).some(concept => concept.id === conceptId && concept.interior_view)) {
    const retained = interiorUnresolvedStates(task, [conceptId]).get(conceptId);
    if (retained) return retained;
  }
  const item = task.job?.items?.[conceptId];
  if (item === 'registration-pending') return 'registration-pending';
  if (item === 'uncertain') return 'uncertain';
  if (task.job?.status === 'running' && (item === 'running' || item === 'queued')) return item;
  if (item === 'failed' || item === 'skipped') return item;
  return task.images.some(image => image.concept_id === conceptId) ? 'image' : 'empty';
}
function inspectInteriorPhoto(event, concepts, selectedImage) {
  if (event.button !== 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  event.preventDefault();
  const owner = task.id, epoch = navigation;
  const photos = interiorRoomGroups(concepts).flatMap(room => room.indices).flatMap(index => {
    const concept = concepts[index], image = latest(concept.id);
    return image ? [{ id: image.id, label: conceptLabel(concept.id), url: imageURL(image.id),
      edit: uploading || planning ? null : () => {
        if (!currentTask(owner, epoch) || !task.images.some(row => row.id === image.id)) return;
        routePush = true; stage = 3; return useImage(image);
      } }] : [];
  });
  openInteriorPhotos({ photos, selectedId: selectedImage.id, opener: event.currentTarget });
}
function interiorRoomSection(room, concepts, cards) {
  const binding = concepts[room.indices[0]]?.interior_view || {};
  return el('section', { class: 'zs-room-results', 'aria-label': room.label,
    'data-space-id': room.id, 'data-layout-id': binding.layout_id || '', 'data-layout-hash': binding.layout_sha256 || '' },
    el('h3', { tabindex: '-1', 'data-focus': `interior-space-room-${task.id}-${concepts[room.indices[0]]?.batch || concepts[room.indices[0]]?.id}-${binding.layout_id || ''}-${binding.layout_sha256 || ''}-${encodeURIComponent(room.id)}` }, room.label),
    el('div', { class: 'zs-reference-results', style: '--result-count:2' }, room.indices.map(index => cards[index])));
}
function interiorRoundOverview(concepts) {
  const overview = interiorSpatialOverview({ concepts, images: task.images,
    disclosureId: `interior-space-map-${task.id}-${concepts[0]?.batch || concepts[0]?.id || 'legacy'}`,
    sourceForLayout: (id, hash) => {
      const record = [task.interior_layout, ...(Array.isArray(task.interior_layout_history) ? task.interior_layout_history : [])].find(row => row?.id === id);
      const binding = concepts.find(concept => concept.interior_view?.layout_id === id
        && concept.interior_view.layout_sha256 === hash)?.interior_view;
      if (!record || !binding || JSON.stringify(record.analysis?.layout) !== JSON.stringify(binding.layout_snapshot)
        || record.analysis.source_sha256 !== record.source_sha256) return null;
      const asset = task.edit_assets?.find(row => row.id === record.source_id && !row.guide_kind
        && row.content_sha256 === record.source_sha256);
      return asset ? base + 'asset/' + task.id + '/' + asset.id : null;
    },
    selectRoom: (spaceId, layoutId, layoutHash) => {
      const room = [...(overview?.parentElement?.querySelectorAll('.zs-room-results') || [])].find(node => node.dataset.spaceId === spaceId
        && node.dataset.layoutId === layoutId && node.dataset.layoutHash === layoutHash);
      if (!room) return;
      const heading = room.querySelector('h3');
      room.scrollIntoView({ block: 'start' }); heading?.focus({ preventScroll: true });
    } });
  return overview;
}
function interiorConceptView() {
  const batches = interiorViewBatches(task.concepts, task.images);
  return [el('div', { class: 'zs-section-head' }, heading('室内视角', '按空间查看整套效果，或打开某张图继续修改。不同轮次分别保留。'),
    el('div', { class: 'zs-row' },
      button('换个风格', changeInteriorStyle, false, busy() || !status.photographic_interior || !cloudImages(), { class: 'zs-btn quiet small' }),
      button('调整参考和要求', () => beginComposition(true), false, busy() || !status.photographic_interior || !cloudImages(), { class: 'zs-btn quiet small' }))),
    ...batches.map((batch, index) => {
      const completed = batch.concepts.filter(concept => latest(concept.id)).length;
      const cards = batch.concepts.map(concept => {
        const result = latest(concept.id), state = cardState(concept.id);
        return el('article', { class: 'zs-reference-result' }, result ? el('a', {
          class: 'zs-photo-preview', href: imageURL(result.id), target: '_blank', rel: 'noopener',
          onclick: event => inspectInteriorPhoto(event, batch.concepts, result),
          'aria-label': '放大查看' + conceptLabel(concept.id) },
          el('img', { src: imageURL(result.id), alt: conceptLabel(concept.id), loading: 'lazy', width: result.width, height: result.height }))
          : el('p', { role: 'status', class: 'zs-help' }, ['running', 'queued'].includes(state) ? '正在生成…'
            : state === 'registration-pending' ? '图片已返回，待登记' : state === 'uncertain' ? '生成结果尚未确认' : '这张还没有生成'),
          el('strong', {}, conceptLabel(concept.id)),
          result && ['running', 'queued', 'registration-pending', 'uncertain'].includes(state)
            ? el('p', { role: 'status', class: 'zs-help' }, state === 'registration-pending' ? '新图已返回，待登记；当前显示之前的图'
              : state === 'uncertain' ? '新图结果尚未确认；当前显示之前的图' : '正在出新图；当前显示之前的图') : null,
          result ? button('在这张上改', () => continueConcept(concept.id, false), false, uploading || planning, { class: 'zs-btn quiet small' }) : null,
          ['empty', 'failed', 'skipped'].includes(state) ? quiet(paidButton('重新出这张', authorization =>
            action('design', { ...authorization, retry_concept_id: concept.id }))) : null);
      });
      return el('details', { class: 'zs-details zs-interior-round', open: index === 0, 'data-disclosure': 'interior-round-' + batch.id },
        el('summary', {}, `第 ${batch.round} 轮 · ${batch.style} · ${completed}/${batch.concepts.length} 个视角`),
        interiorSetControls(batch.id, completed, batch.concepts.length),
        resumeInteriorButton(batch.id),
        interiorRoundOverview(batch.concepts),
        ...interiorRoomGroups(batch.concepts).map(room => interiorRoomSection(room, batch.concepts, cards)));
    }),
    button(`查看全部图片（${task.images.length}）`, () => { endComposition(); galleryOpen = true; go(3); }, false, false, { class: 'zs-btn quiet small' })];
}
function conceptView() {
  if (task.interior_input && task.concepts.some(concept => concept.interior_view)) return interiorConceptView();
  if (task.interior_input && !task.concepts.length) return [heading('室内视角', '完成的照片会按空间显示。'),
    busy() ? empty('室内效果正在准备', '平面图已保存，可以先离开，稍后回来查看。')
      : empty('还没有室内效果图', '平面图和风格已保存，可以继续生成。', button('继续生成室内效果', () => beginComposition(true), true, false))];
  if (!task.concepts.length) return [heading('方案', task.brief.title),
    busy() ? el('div', { class: 'zs-waiting', role: 'status' }, el('span', { class: 'zs-spinner', 'aria-hidden': 'true' }),
      el('div', {}, el('strong', {}, '正在构思方案…'), el('p', {}, task.job?.render ? '关掉页面也会继续。三个方案出来后会接着出效果图。' : '关掉页面也会继续，完成后三个方案会出现在这里。')))
      : empty('还没有方案', '到构思页开始，或者上传一张现场照片。', button('去构思', () => go(1), true, false))];
  const shown = task.concepts.filter(c => !c.source_image_id);
  const uploaded = task.concepts.filter(c => c.source_image_id);
  const { box, summary } = settings();
  const rendered = shown.some(c => latest(c.id));
  const cards = shown.map((c, index) => {
    const image = latest(c.id), chosen = task.selected === c.id, state = cardState(c.id);
    const waiting = state === 'running' || state === 'queued', pending = state === 'registration-pending';
    const label = { running: '正在出图…', queued: '排队中，稍后出图', failed: '这张没有生成', skipped: '已停止，这张没有生成',
      'registration-pending': '图片已保存，待登记；请先恢复已有结果', uncertain: '这张的结果还没确认，先别重出', empty: '还没有效果图' }[state];
    const tag = waiting ? '正在出新图…' : { failed: '这次没有生成，仍是之前的图', skipped: '已停止，仍是之前的图',
      'registration-pending': '新图已保存，待登记；当前显示之前的图', uncertain: label }[state];
    const media = image
      ? [el('img', { src: imageURL(image.id), alt: c.name + ' 的效果图', loading: 'lazy', width: image.width, height: image.height }),
        tag ? el('span', { class: 'zs-card-tag' + (['failed', 'uncertain'].includes(state) || pending ? ' warn' : '') }, tag) : null]
      : el('div', { class: 'zs-unrendered' + (['failed', 'uncertain'].includes(state) || pending ? ' zs-failed' : ''), role: waiting || pending ? 'status' : null },
        state === 'running' ? el('span', { class: 'zs-spinner', 'aria-hidden': 'true' }) : icon('photo'), el('span', {}, label));
    const retry = c.reference_design ? (state === 'uncertain' || waiting || pending ? null : quiet(paidButton(image ? '再出一张' : '重新出这张',
      authorization => action('design', { ...authorization, retry_concept_id: c.id }))))
      : c.composition ? button('调整组合', beginComposition, false, busy(), { class: 'zs-btn quiet small' }) : state === 'uncertain' || waiting || pending ? null : quiet(paidButton(state === 'failed' ? '重新出这张' : image ? '再生成一张' : '生成效果图',
      authorization => action('generate', { ...getSettings(), ...authorization, concept_ids: [c.id] })));
    const use = button(chosen ? '继续改图' : '用这个方案', chosen ? () => continueConcept(c.id) : chooseConcept(c.id), Boolean(image) || chosen);
    return el('article', { class: 'zs-concept' + (chosen ? ' chosen' : ''), 'aria-labelledby': 'zs-concept-' + c.id },
      el('div', { class: 'zs-concept-image', 'data-state': state }, media),
      el('div', { class: 'zs-concept-body' },
        el('p', { class: 'zs-concept-no' }, conceptLabel(c.id) + (chosen ? '（已选方案）' : '')),
        el('h3', { id: 'zs-concept-' + c.id }, c.name), el('p', { class: 'zs-concept-text' }, c.concept),
        c.brief_revision && c.brief_revision !== (task.brief_revision || 1)
          ? el('p', { class: 'zs-help' }, '按旧版要求构思。再次出图仍用当时的要求；如需采用新要求，请重新构思。') : null,
        el('details', {}, el('summary', {}, '形态、材质和说明'),
          el('dl', {}, el('dt', {}, '形态'), el('dd', {}, c.form), el('dt', {}, '材质建议'), el('dd', {}, c.materials)),
          c.rationale ? el('p', {}, c.rationale) : null, c.risks?.length ? el('ul', {}, c.risks.map(r => el('li', {}, r))) : null),
        el('div', { class: 'zs-concept-actions' }, use, retry)));
  });
  const batch = paidButton(rendered ? '再出一组图' : `给 ${Math.min(3, shown.length)} 个方案各出一张图`, authorization =>
    action('generate', { ...getSettings(), ...authorization, concept_ids: shown.filter(c => !c.composition).slice(-3).map(c => c.id) }));
  quiet(batch);
  const description = rendered ? '比较各个方案，挑中一个就可以接着修改。'
    : task.job?.status === 'running' ? '方案已经出来，效果图正在生成，会陆续出现在各自的卡片上。' : '先看说明。想比较外观，可以给方案出效果图。';
  return [el('div', { class: 'zs-section-head' }, heading('挑一个方案', description),
      task.workspace === 'reference_design' ? button('调整参考，再出方案', () => beginComposition(true), false, busy(), { class: 'zs-btn quiet small' })
        : button('重新构思…', () => go(1), false, busy(), { class: 'zs-btn quiet small' })),
    el('div', { class: 'zs-concept-tools' }, shown.some(c => !c.composition) ? batch : null, el('details', { class: 'zs-output-options' }, el('summary', {}, '图片尺寸和比例', summary), box), blocker()),
    el('div', { class: 'zs-concept-grid' }, cards),
    uploaded.length ? el('section', { class: 'zs-uploaded-directions' }, el('h3', {}, '另外上传的底图'),
      el('div', { class: 'zs-row' }, uploaded.map(c => button(conceptLabel(c.id), chooseConcept(c.id), false, busy())))) : null,
    button(`查看全部图片（${task.images.length}）`, () => { galleryOpen = true; go(3); }, false, false)];
}
function conceptLabel(id) {
  const interior = task.concepts.find(c => c.id === id && c.interior_photographic);
  if (interior) return interior.name;
  const reference = task.concepts.filter(c => c.reference_design).findIndex(c => c.id === id);
  if (reference >= 0) return `参考方案 ${reference + 1}`;
  const composition = task.concepts.filter(c => c.composition && !c.reference_design).findIndex(c => c.id === id);
  if (composition >= 0) return `组合 ${composition + 1}`;
  const planned = task.concepts.filter(c => !c.source_image_id && !c.composition), uploaded = task.concepts.filter(c => c.source_image_id);
  const index = planned.findIndex(c => c.id === id);
  return index >= 0 ? `方案 ${index + 1}` : `上传图 ${uploaded.findIndex(c => c.id === id) + 1}`;
}
function fileName(image) {
  const title = task.brief.title.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '').trim().slice(0, 50) || '设计图';
  return `${title}-${conceptLabel(image.concept_id).replace(' ', '')}-版本${image.version}-${image.id.slice(0, 8)}.${image.file?.split('.').at(-1) || 'png'}`;
}
function versionNote(image, images) {
  if (image.floorplan_input?.mode === 'floorplan_photographic') return '原始平面图 · ' + image.floorplan_input.style_label;
  if (image.kind === 'design') return image.edits?.length ? `根据 ${image.edits.length} 份参考设计` : '参考设计 · 历史份数未记录';
  if (image.kind === 'compose') return image.edits?.length ? `由 ${image.edits.length} 份参考组合` : '多图组合 · 历史份数未记录';
  const parent = images.find(i => i.id === image.parent_id);
  if (image.kind === 'upload' || !parent) return kindLabel[image.kind] || '图片';
  return image.kind === 'export' ? `版本 ${parent.version} 的高清图` : image.kind === 'refine' ? `由版本 ${parent.version} 修改` : `由版本 ${parent.version} 生成`;
}
function versionStrip(images, active, onPick, label = '版本') {
  const owners = new Set(images.map(image => image.concept_id));
  const running = task.job?.status === 'running' && ['generate', 'refine'].includes(task.job.action) &&
    Object.entries(task.job.items || {}).some(([id, state]) => ['running', 'queued'].includes(state) &&
      (owners.has(id) || task.images.some(image => image.id === id && owners.has(image.concept_id))));
  return el('div', { class: 'zs-thumbnails', role: 'group', 'aria-label': label },
    images.map(i => el('button', { class: 'zs-thumb', type: 'button', 'data-image-id': i.id, 'data-focus': 'thumb-' + i.id,
      'aria-current': String(i.id === active?.id), 'aria-label': `版本 ${i.version}，${versionNote(i, images)}`, onclick: () => { supersedeRouteLoad(); onPick(i.id); } },
      el('img', { src: imageURL(i.id), alt: '', loading: 'lazy', width: i.width, height: i.height }),
      el('span', {}, `${i.version}${i.kind === 'upload' ? ' 原图' : ''}${i.exports?.length ? ' 已保存' : ''}`),
      i.parent_id ? el('small', {}, versionNote(i, images)) : null,
      task.archives.some(a => a.image_id === i.id) ? el('small', {}, '已存档') : null)),
    running && label === '版本' ? el('div', { class: 'zs-thumb pending', role: 'status' }, el('span', { class: 'zs-spinner', 'aria-hidden': 'true' }), el('span', {}, '生成中')) : null);
}
function currentEdit(image) {
  const key = task.id + ':' + image.id;
  if (!editDrafts.has(key)) {
    let saved, readError = '';
    try {
      saved = JSON.parse(sessionStorage.getItem('zyb.studio.edits.' + key) || 'null');
      if (!saved && image.id !== 'compose') {
        const old = JSON.parse(sessionStorage.getItem('zyb.studio.edits.' + task.id) || 'null');
        if (old?.imageId === image.id) saved = old;
      }
      if (saved !== null && saved !== undefined && (typeof saved !== 'object' || Array.isArray(saved) || !Array.isArray(saved.rows)
        || saved.rows.length > maxEditRows || (saved.imageId && saved.imageId !== image.id)
        || saved.rows.some(row => !row || !['upload', 'image', 'reference', 'material'].includes(row.source)
          || typeof row.id !== 'string' || !/^[a-zA-Z0-9_-]{1,80}$/.test(row.id))
        || (saved.suppressed !== undefined && !Array.isArray(saved.suppressed))
        || (saved.count !== undefined && ![1, 2, 3].includes(saved.count)))) throw Error('invalid edit draft');
    } catch { saved = null; readError = '旧参考草稿未能读取，未覆盖旧记录。'; }
    const rows = Array.isArray(saved?.rows) ? saved.rows.filter(row => row && ['upload', 'image', 'reference', 'material'].includes(row.source) &&
      typeof row.id === 'string' && /^[a-zA-Z0-9_-]{1,80}$/.test(row.id)).slice(0, maxEditRows) : [];
    for (const row of rows) if ((row.use !== 'object' || row.source === 'material') && row.fidelity === 'original') row.fidelity = 'reference';
    const suppressed = Array.isArray(saved?.suppressed) ? saved.suppressed
      .filter(id => typeof id === 'string' && /^[a-zA-Z0-9_-]{1,80}$/.test(id)).slice(0, 100) : [];
    const restoredDraft = { ...saved, rows, referencesTouched: saved?.referencesTouched ?? Boolean(saved),
      suppressed: new Set(suppressed), query: '', matches: [], loading: false };
    delete restoredDraft.storageState;
    Object.defineProperty(restoredDraft, 'storageState', {
      value: { error: readError, blocked: Boolean(readError), dirty: false, restored: Boolean(saved) } });
    editDrafts.set(key, restoredDraft);
    if (!saved && image.id === 'compose' && task.workspace === 'reference_design') {
      const draft = editDrafts.get(key);
      draft.designMode = true; draft.count = task.floorplan || task.interior_input ? 1 : 3;
      const ids = task.floorplan ? new Set([task.floorplan.source_id, ...task.floorplan.guides.map(row => row.id)]) : null;
      draft.rows = (task.edit_assets || []).filter(asset => !asset.internal_layout && (!ids || ids.has(asset.id))).slice(0, maxEditRows).map(asset => ({ source: 'upload', id: asset.id,
        name: asset.name, preview: base + 'asset/' + task.id + '/' + asset.id, use: task.floorplan ? 'shape' : '',
        take: task.floorplan ? '保持已确认的墙体、开口、功能区与相机视角，只调整材质和光线。' : '' }));
      if (task.interior_input) draft.rows = (task.interior_input.edits || []).map(row => ({ ...row,
        preview: row.source === 'upload' ? base + 'asset/' + task.id + '/' + row.id : base + 'reference-image/' + row.id }));
      draft.referencesTouched = draft.rows.length > 0;
    }
    if (!saved) {
      const draft = editDrafts.get(key);
      const info = new Map((task.reference_info || []).map(ref => [ref.id, ref]));
      for (const ref of task.references || []) {
        if (draft.rows.length >= maxEditRows) continue;
        if (draft.rows.some(row => row.source === 'reference' && row.id === ref.id)) continue;
        const packRef = stylePacks.flatMap(pack => pack.references).find(row => row.id === ref.id);
        const detail = info.get(ref.id);
        if (detail ? detail.kind !== 'image' : !packRef && ref.role !== '风格' && !(ref.use && !stylePacks.length)) continue;
        const use = ref.use || referenceUse(ref.role);
        draft.rows.push({ source: 'reference', id: ref.id, name: detail?.title || packRef?.title || `已保存的${ref.role}参考`,
          preview: base + 'reference-image/' + ref.id, use, fidelity: 'reference',
          source_selection: ref.source_selection || null, source_sha256: ref.source_sha256 || '',
          take: ref.take ?? (use === 'style' ? '只参考材质搭配、装饰层次和光线，不照搬人物或宗教细节。' : '') });
      }
      draft.referencesTouched = draft.rows.length > 0;
    }
  }
  const draft = editDrafts.get(key);
  if (image.id === 'compose' && task.interior_input) draft.designMode = true;
  if (!draft.storageState) Object.defineProperty(draft, 'storageState', { value: { error: '', blocked: false, dirty: false } });
  if (draft.imageId !== image.id) {
    const keepRoomTargets = image.id === 'compose' && task.interior_input && !draft.imageId;
    for (const row of draft.rows) {
      if (!keepRoomTargets) row.target = '';
      row.region = null; row.target_selection = null; row.auto = false;
    }
    draft.selection = null; draft.selection_proof = null; draft.mode = draft.mode || 'full';
    draft.imageId = image.id;
  }
  return draft;
}
function saveEditDraft(image) {
  const key = task.id + ':' + image.id, draft = currentEdit(image);
  const state = draft.storageState;
  state.dirty = true;
  if (!state.blocked) {
    try {
      sessionStorage.setItem('zyb.studio.edits.' + key, JSON.stringify({ imageId: draft.imageId, rows: draft.rows,
        suppressed: [...draft.suppressed],
        referencesTouched: draft.referencesTouched, selection: draft.selection, selection_proof: draft.selection_proof, mode: draft.mode,
        designMode: draft.designMode, count: draft.count }));
      state.error = ''; state.dirty = false;
    } catch { state.error = '本标签页未能暂存参考设置。'; }
  }
  if (typeof refreshEditDraftNotices === 'function') refreshEditDraftNotices();
  return { ok: !state.error, error: state.error };
}
function retryEditDraft(image, reread = false) {
  if (!reread) return saveEditDraft(image);
  const key = task.id + ':' + image.id, previous = currentEdit(image);
  if (!window.confirm('重新读取会替换当前页面的参考填写。服务器记录和历史图片不变。是否继续？')) return { ok: false, cancelled: true };
  editDrafts.delete(key);
  const recovered = currentEdit(image);
  if (recovered.storageState.blocked) {
    editDrafts.set(key, previous); previous.storageState.error = recovered.storageState.error;
    refreshEditDraftNotices();
    return { ok: false, error: previous.storageState.error };
  }
  render(); tell(recovered.storageState.restored ? '已重新读取本标签页的参考草稿，请核对内容；尚未提交。'
    : '未找到旧参考草稿，已显示当前设计的默认参考，请重新核对。');
  return { ok: true };
}
function replaceUnreadEditDraft(image) {
  const draft = currentEdit(image), state = draft.storageState;
  if (!state.blocked) return saveEditDraft(image);
  if (!window.confirm('旧草稿尚未读出。继续会用当前页面的参考填写替换本标签页中这份草稿。服务器记录和历史图片不变。是否替换？'))
    return { ok: false, cancelled: true };
  state.blocked = false;
  const result = saveEditDraft(image);
  if (!result.ok) { state.blocked = true; refreshEditDraftNotices(); }
  return result;
}
function refreshEditDraftNotices() {
  if (typeof view === 'undefined' || !view) return;
  for (const node of view.querySelectorAll('[data-edit-draft-notice]')) node.refreshDraft?.();
}
function editDraftNotice(image) {
  const owner = task.id, key = owner + ':' + image.id;
  const notice = el('section', { class: 'zs-upload-recovery', 'data-edit-draft-notice': key,
    'aria-label': '参考草稿暂存', 'aria-live': 'polite', hidden: true });
  notice.refreshDraft = () => {
    const state = editDrafts.get(key)?.storageState;
    notice.hidden = !state?.error;
    if (!state?.error) { notice.replaceChildren(); return; }
    const run = (action, saved = true) => {
      if (task?.id !== owner) return;
      const result = action();
      if (result.ok && saved) tell('本标签页的参考草稿已暂存，尚未提交。');
    };
    notice.replaceChildren(el('p', { class: 'zs-help', role: 'status' }, state.error
      + ' 未提交的参考填写仅在页面内，刷新或关页会丢失。'),
      el('div', { class: 'zs-row' }, state.blocked
        ? [button('重新读取旧草稿', () => run(() => retryEditDraft(image, true), false), false, false, { class: 'zs-btn quiet small' }),
          button('用当前填写替换旧草稿…', () => run(() => replaceUnreadEditDraft(image)), false, false, { class: 'zs-btn quiet small' })]
        : button('重试暂存参考草稿', () => run(() => retryEditDraft(image)), false, false, { class: 'zs-btn quiet small' })));
  };
  notice.refreshDraft();
  return notice;
}
function selectedEdits(image) {
  return currentEdit(image).rows.map(({ source, id, target = '', region = null, use = 'reference', take = '', fidelity = 'reference',
    source_selection = null, target_selection = null, source_sha256 = '' }) => ({ source, id, target, region, use, take, fidelity,
      source_selection, target_selection, ...(source_selection && source_sha256 ? { source_sha256 } : {}) }));
}
function referenceBudgetPayload(image) {
  const draft = currentEdit(image);
  return { revision: task.revision, mode: image.id === 'compose' ? draft.designMode ? 'design' : 'compose' : 'refine',
    ...(image.id !== 'compose' ? { image_id: image.id } : {}),
    edits: selectedEdits(image).map(row => ({ source: row.source, id: row.id,
      ...(row.source_selection ? { source_selection: row.source_selection, source_sha256: row.source_sha256 || '' } : {}) })) };
}
function referenceBudgetState(image) {
  const payload = referenceBudgetPayload(image), key = task.id + ':' + image.id, signature = JSON.stringify(payload);
  const previous = referenceBudgets.get(key);
  if (task.job?.status === 'running') {
    if (previous?.timer) clearTimeout(previous.timer);
    previous?.finish?.();
    if (previous) previous.state = 'running';
    return { signature, state: 'running', budget: null, error: '' };
  }
  if (previous?.refreshing && previous.ownerView === view) return previous;
  if (previous?.signature === signature && !previous.refreshing && previous.state !== 'running') return previous;
  if (previous?.timer) clearTimeout(previous.timer);
  previous?.finish?.();
  const owner = task.id;
  const entry = { signature, state: 'loading', budget: null, error: '' };
  entry.ready = new Promise(resolve => { entry.finish = resolve; });
  referenceBudgets.set(key, entry);
  entry.timer = setTimeout(async () => {
    if (task?.id === owner && task.job?.status === 'running') { entry.state = 'running'; entry.finish(); return; }
    try {
      const budget = await api('tasks/' + owner + '/reference-budget', payload);
      if (![budget.total, budget.base_count, budget.structure_count, budget.reference_count, budget.plan_count || 0].every(n => Number.isInteger(n) && n >= 0)
        || !Number.isInteger(budget.maximum) || budget.maximum < 1 || budget.revision !== payload.revision
        || budget.total !== budget.base_count + budget.structure_count + budget.reference_count + (budget.plan_count || 0)
        || budget.within_limit !== (budget.total <= budget.maximum)) throw Error('图片计数未能确认，请重试。');
      entry.budget = budget; entry.state = 'ready';
    } catch (error) { entry.error = userMessage(error.message); entry.state = 'error'; }
    entry.finish();
    if (panel?.open && task?.id === owner && referenceBudgets.get(key) === entry) {
      refreshReferenceBudgets(); updateActionButtons(); refreshValidationNotice();
    }
  }, 180);
  return entry;
}
async function retryReferenceBudget(image) {
  const owner = task.id, epoch = navigation, ownerView = view, revision = task.revision;
  const key = owner + ':' + image.id, previous = referenceBudgets.get(key);
  if (previous?.refreshing && previous.ownerView === ownerView) return;
  if (previous?.timer) clearTimeout(previous.timer);
  const entry = { signature: JSON.stringify(referenceBudgetPayload(image)), state: 'loading', refreshing: true,
    ownerView, budget: null, error: '' };
  referenceBudgets.set(key, entry);
  refreshReferenceBudgets(); updateActionButtons(); refreshValidationNotice();
  try {
    const fresh = await api('tasks/' + owner);
    if (!currentTask(owner, epoch) || view !== ownerView || referenceBudgets.get(key) !== entry) return;
    if (!fresh || fresh.id !== owner || !Number.isSafeInteger(fresh.revision) || fresh.revision < revision
      || !Array.isArray(fresh.images) || !Array.isArray(fresh.references) || !Array.isArray(fresh.concepts)
      || !fresh.brief || typeof fresh.brief !== 'object' || Array.isArray(fresh.brief)) throw Error('设计记录未能确认，请重新核对。');
    if (fresh.revision >= task.revision) task = fresh;
    tasks = tasks.map(row => row.id === owner && (row.revision || 0) <= task.revision ? task : row);
    referenceBudgets.delete(key);
    render();
    tell('设计记录已刷新；本次参考和说明仍未提交，请重新核对。');
  } catch (error) {
    if (!currentTask(owner, epoch) || view !== ownerView || referenceBudgets.get(key) !== entry) return;
    entry.refreshing = false; entry.signature = JSON.stringify(referenceBudgetPayload(image));
    entry.state = 'error'; entry.error = userMessage(error.message);
    refreshReferenceBudgets(); updateActionButtons(); refreshValidationNotice();
  } finally {
    if (entry.refreshing && referenceBudgets.get(key) === entry) {
      referenceBudgets.delete(key);
      if (panel?.open && task?.id === owner) {
        refreshReferenceBudgets(); updateActionButtons(); refreshValidationNotice();
      }
    }
  }
}
function referenceBudgetIssue(image) {
  if (!cloudImages()) return '';
  const entry = referenceBudgetState(image);
  if (entry.state === 'running') return '';
  if (entry.state === 'loading') return '正在准备参考图片…';
  if (entry.state === 'error') return '参考图片暂未准备好，请在参考区重试。';
  return entry.budget.within_limit ? '' : `本次共 ${entry.budget.total} 张图片，最多 ${entry.budget.maximum} 张。请移除不需要的参考；石材卡片可能包含多张样图。`;
}
function refreshReferenceBudgets() {
  for (const node of view?.querySelectorAll('[data-reference-budget]') || []) node.refreshBudget?.();
}
function referenceBudgetView(image) {
  const text = el('p', { class: 'zs-help', role: 'status' });
  const retry = button('重新载入参考', () => retryReferenceBudget(image), false, false, { class: 'zs-btn quiet small' });
  const node = el('div', { class: 'zs-reference-budget', 'data-reference-budget': image.id }, text, retry);
  node.refreshBudget = () => {
    const entry = referenceBudgetState(image), budget = entry.budget;
    retry.hidden = entry.state !== 'error';
    node.hidden = entry.state === 'running' || entry.state === 'ready' && budget.within_limit;
    text.textContent = entry.state === 'running' ? '正在生成图片，参考设置稍后可继续调整。'
      : entry.state === 'loading' ? '正在准备参考图片…'
      : entry.state === 'error' ? '参考图片暂未载入，已有选择保留。' + entry.error
      : budget.within_limit ? `本次已选 ${budget.total} 张图片。`
      : `本次共有 ${budget.total} 张图片，生成服务最多接收 ${budget.maximum} 张，请移除不需要的参考。`;
    node.dataset.overLimit = String(entry.state === 'ready' && !budget.within_limit);
  };
  node.refreshBudget();
  return node;
}
function addMaterial(image, source, id, name, preview, auto = false) {
  const draft = currentEdit(image);
  const row = draft.rows.find(v => v.source === source && v.id === id);
  if (!row) {
    if (draft.rows.length >= maxEditRows) throw Error('参考卡片过多，请先移除不需要的卡片。');
    draft.rows.push({ source, id, name, preview, target: '', take: '',
      use: source === 'material' ? 'material' : draft.designMode ? image.id === 'compose' && task.interior_input ? 'style' : '' : 'reference',
      fidelity: 'reference', auto });
  }
  else if (!auto) row.auto = false;
  draft.referencesTouched = true;
  saveEditDraft(image);
}
function mentionsMaterial(text, product, positiveOnly = true) {
  return [product.name, ...product.aliases].some(alias => {
    let at = text.indexOf(alias);
    while (at >= 0) {
      const before = text.slice(Math.max(0, at - 8), at), after = text.slice(at + alias.length);
      if (!(alias === '敦煌壁' && after.startsWith('画')) && (!positiveOnly ||
        (!/(?:不|别|除了|去掉|移除|取消)/.test(before) && !/^[\s，,]*(?:不要|不用|别用|不选|不需要)/.test(after)))) return true;
      at = text.indexOf(alias, at + alias.length);
    }
    return false;
  });
}
async function attachMaterial(file, image) {
  return attachReferences([file], image);
}
async function attachReferences(files, image) {
  const waiting = pendingUploads.get(task.id + ':references');
  if (waiting && (waiting.imageId !== image.id || waiting.files !== files))
    throw Error('还有参考照片没有传完。请先返回原底图继续上传，或选择不再上传剩余照片。');
  files = [...files];
  if (!files.length || busy()) return;
  files.forEach(validateUpload);
  const id = task.id, epoch = navigation, previous = image.id, original = task;
  const draft = currentEdit(image);
  const room = maxEditRows;
  uploading = true; render();
  try {
    let missing = 0;
    for (const file of files) {
      const asset = await matchingMaterial(original, { file });
      if (!currentTask(id, epoch)) return;
      if (!asset || !draft.rows.some(row => row.source === 'upload' && row.id === asset.id)) missing++;
    }
    if (missing + draft.rows.length > room) throw Error('参考图过多，请先移除不需要的图片。');
    for (const [index, file] of files.entries()) {
      if (!currentTask(id, epoch)) return;
      pendingUploads.set(id + ':references', { files: files.slice(index), imageId: image.id });
      const item = { file, mode: 'reference' };
      let saved;
      try {
        const asset = await matchingMaterial(task, item);
        if (!currentTask(id, epoch)) return;
        saved = asset ? task : await uploadImage(id, task.revision, item);
      }
      catch (error) {
        const fresh = await api('tasks/' + id).catch(() => null);
        if (!fresh || !error.uploadUncertain || !await matchingMaterial(fresh, item)) {
          if (fresh && currentTask(id, epoch) && fresh.revision >= task.revision) task = fresh;
          throw error;
        }
        saved = fresh;
      }
      if (!currentTask(id, epoch)) return;
      const asset = await matchingMaterial(saved, item);
      if (!asset) throw Error('照片已上传，请在“上传过的照片”里选择。');
      if (!currentTask(id, epoch)) return;
      if (saved.revision >= task.revision) task = saved;
      addMaterial(image, 'upload', asset.id, asset.name || '参考照片', base + 'asset/' + id + '/' + asset.id);
    }
    pendingUploads.delete(id + ':references');
    if (previous !== 'compose') imageSelection.set(id, previous);
    tell('参考照片已添加。' + (previous === 'compose' ? '' : '底图没有变。'));
  } finally { uploading = false; if (currentTask(id, epoch)) render(); }
}
async function matchingMaterial(saved, item) {
  if (!globalThis.crypto?.subtle) throw Error('请在这台电脑的本机地址或 HTTPS 页面添加照片。');
  item.sha256 ||= [...new Uint8Array(await crypto.subtle.digest('SHA-256', await item.file.arrayBuffer()))]
    .map(byte => byte.toString(16).padStart(2, '0')).join('');
  return saved.edit_assets?.find(v => v.source_sha256 === item.sha256) || null;
}
function referenceUploadRecovery(image) {
  const pending = pendingUploads.get(task.id + ':references');
  if (!pending) return null;
  const same = pending.imageId === image.id;
  return el('div', { class: 'zs-upload-recovery', role: 'status' },
    el('p', { class: 'zs-help' }, same ? pending.references ? pending.files.length ? '参考资料尚未保存，照片也尚未上传。继续时会先保存资料，再上传照片。'
      : '底图已保留，已选参考资料尚未保存。可以继续保存资料，不会重新上传底图。' : '还有照片没有传完，已上传的已保留。'
      : '另一张底图还有参考照片没有传完。请返回原底图继续，不会添加到当前图片。'),
    button(same ? pending.files.length ? '继续上传' : '继续保存资料' : '返回原底图', async () => {
      if (same) {
        if (pending.references) {
          uploading = true; render();
          try {
            if (!await action('references', { references: pending.references })) return;
            delete pending.references;
          } finally { uploading = false; if (panel?.open) render(); }
        }
        if (!pending.files.length) {
          pendingUploads.delete(task.id + ':references'); render();
          tell('已选参考资料已保存，底图没有变化。'); return;
        }
        return attachReferences(pending.files, image);
      }
      if (pending.imageId === 'compose') return beginComposition(currentEdit({ id: 'compose' }).designMode);
      const original = task.images.find(row => row.id === pending.imageId);
      if (!original) throw Error('原底图暂时找不到，请核对这份设计的图片记录。剩余照片没有重传。');
      return useImage(original);
    }, false, busy(), { class: 'zs-btn small' }),
    button(pending.files.length ? '不再上传剩余照片' : '取消本次补存', () => { pendingUploads.delete(task.id + ':references'); render(); }, false, busy(), { class: 'zs-btn quiet small' }));
}
function materialEditor(image, change) {
  const draft = currentEdit(image), choices = el('div', { class: 'zs-material-choices' });
  const composing = image.id === 'compose';
  const suggestion = el('p', { class: 'zs-help' });
  const retained = el('p', { class: 'zs-help zs-retained-references' });
  const rows = el('div', { class: 'zs-edit-rows' });
  const choosePart = (row, target = false) => {
    const owner = task.id, epoch = navigation;
    const reference = target ? { source: 'image', id: image.id } : { source: row.source, id: row.id };
    openSelection({ url: target ? imageURL(image.id) : row.source === 'reference' ? base + 'reference-source/' + row.id : row.preview,
      initial: target ? row.target_selection : row.source_selection,
      title: target ? `参考图 ${draft.rows.indexOf(row) + 1} 用在哪里` : `参考图 ${draft.rows.indexOf(row) + 1} 取哪部分`,
      owner, reference, api, segmentReady: status.segment_ready, intent: target ? 'target' : 'source',
      commit: (selection, proof) => {
        if (!currentTask(owner, epoch)) return;
        row[target ? 'target_selection' : 'source_selection'] = selection;
        if (!target) row.source_sha256 = proof?.image_sha256 || '';
        if (target) {
          draft.mode = 'local';
        }
        saveEditDraft(image); if (target) render(); else paint();
      } });
  };
  const paint = () => {
    const focus = focusKey(document.activeElement);
    const included = new Set(draft.rows.filter(row => row.source === 'reference').map(row => row.id));
    const excluded = (task.references || []).filter(ref => !included.has(ref.id)).length;
    retained.hidden = !excluded;
    retained.textContent = excluded ? `还有 ${excluded} 份已选资料仅保存在设计记录中，不参与本次图片生成；实际参考以图片卡片为准。` : '';
    for (const details of rows.querySelectorAll('details[data-disclosure]')) {
      const row = draft.rows.find(row => details.dataset.disclosure === `reference-${row.source}-${row.id}`);
      if (row) row.optionsOpen = details.open;
    }
    choices.replaceChildren(...materials.map(m => button(m.name, () => {
      addMaterial(image, 'material', m.id, m.name, m.references[0] ? base + 'reference-image/' + m.references[0].id : '');
      draft.suppressed.delete(m.id); saveEditDraft(image); paint();
    }, false, busy() || !m.available, { class: 'zs-material-choice', 'aria-pressed': String(draft.rows.some(v => v.source === 'material' && v.id === m.id)) })));
    [...choices.children].forEach((node, i) => {
      const ref = materials[i].references[0];
      if (ref) node.prepend(el('img', { src: base + 'reference-image/' + ref.id, alt: '', loading: 'lazy' }));
      node.append(el('small', {}, `含 ${materials[i].references.length} 张样图`));
      if (materials[i].aliases?.length) node.append(el('small', {}, materials[i].aliases.filter(x => x !== materials[i].name).join(' / ')));
    });
    rows.replaceChildren(...draft.rows.map((row, i) => {
      const boundPlan = composing && task.interior_input?.source_id === row.id && row.source === 'upload';
      if (boundPlan) return el('article', { class: 'zs-edit-row', 'aria-label': `参考图 ${i + 1}` },
        el('div', { class: 'zs-row' }, el('a', { href: row.preview, target: '_blank', rel: 'noopener', 'aria-label': '查看原始平面图' },
          el('img', { src: row.preview, alt: '原始平面图' })), el('strong', {}, '原始平面图 · 布局依据')),
      el('p', { class: 'zs-help' }, '用这张平面图确定空间布局。还可以添加喜欢的风格、材质或家具照片。'));
      const target = el('input', { type: 'text', value: row.target || '', placeholder: '放在哪里？例如：底座或正面墙板（可选）',
        'aria-label': `参考图 ${i + 1} 的使用位置`, 'data-focus': 'material-target-' + row.id, disabled: busy(), maxlength: '160',
        oninput: event => { row.target = event.target.value; row.auto = false; saveEditDraft(image); } });
      const take = el('input', { type: 'text', value: row.take || '', placeholder: '想参考哪部分？也可统一写在修改说明里',
        'aria-label': `参考图 ${i + 1} 取用内容`, disabled: busy(), maxlength: '160',
        oninput: event => { row.take = event.target.value; row.auto = false; saveEditDraft(image); } });
      const use = el('select', { 'aria-label': `参考图 ${i + 1} 用途`, disabled: busy() || boundPlan,
        onchange: event => { setInteriorReferenceUse(row, event.target.value); row.auto = false; if (row.use !== 'object') row.fidelity = 'reference'; saveEditDraft(image); paint(); } },
        [...(composing && draft.designMode ? [['', '请选择用途']] : []), ['reference', '按说明参考'], ['object', '物件'], ['shape', '造型'], ['material', '材质'], ['pattern', '纹样'], ['style', '风格']]
          .map(([value, label]) => el('option', { value }, label)));
      use.value = [...use.options].some(option => option.value === row.use) ? row.use : 'reference';
      const automaticName = /^(参考照片|上传材质)\s*\d*$/.test(row.name || '');
      const name = row.source === 'upload' ? el('input', { type: 'text', value: automaticName ? '' : row.name, placeholder: '给照片起个名字（可选）', maxlength: '80', 'aria-label': `参考图 ${i + 1} 名称`, disabled: busy(),
        onchange: async event => {
          const owner = task.id, epoch = navigation, value = event.target.value.trim();
          if (!value) { event.target.value = automaticName ? '' : row.name; return; }
          try {
            const saved = await api('tasks/' + owner + '/asset-name', { asset_id: row.id, name: value });
            if (currentTask(owner, epoch)) { if (saved.revision >= task.revision) task = saved; row.name = value; saveEditDraft(image); paint(); }
          } catch (error) { if (currentTask(owner, epoch)) { event.target.value = automaticName ? '' : row.name; tell(error.message, true); } }
        } }) : null;
      const original = el('input', { type: 'checkbox', checked: row.fidelity === 'original', disabled: busy(),
        onchange: event => { row.fidelity = event.target.checked ? 'original' : 'reference'; saveEditDraft(image); updateActionButtons(); } });
      const single = row.source !== 'material';
      return el('article', { class: 'zs-edit-row', 'aria-label': `参考图 ${i + 1}` }, el('div', { class: 'zs-row' },
        row.preview ? el('a', { href: row.preview, target: '_blank', rel: 'noopener', 'aria-label': `查看参考图 ${i + 1}` },
          el('img', { src: row.preview, alt: `参考图 ${i + 1}` })) : null,
        el('div', { class: 'zs-ref-title' }, el('strong', {}, boundPlan ? '原始平面图 · 布局依据' : `参考图 ${i + 1}`), !automaticName && row.name ? el('small', {}, row.name) : null,
          row.source === 'material' && materials.find(m => m.id === row.id) ? el('small', {}, `含 ${materials.find(m => m.id === row.id).references.length} 张样图`) : null),
        boundPlan ? null : button('移除', () => { draft.rows.splice(i, 1); draft.referencesTouched = true; draft.suppressed.add(row.id); saveEditDraft(image); paint(); }, false, busy(), { class: 'zs-btn quiet small' })),
        composing && draft.designMode ? el('label', { class: 'zs-field' }, el('span', {}, '参考用途'), use) : null,
        el('label', { class: 'zs-ref-take' }, el('span', {}, '参考哪部分（可选）'), take),
        el('div', { class: 'zs-ref-actions' },
          single && !boundPlan ? button(row.source_selection ? '调整取用范围' : '选取局部', () => choosePart(row), false, busy(), { class: 'zs-btn quiet small' }) : null,
          row.source_selection ? button('参考整张', () => { row.source_selection = null; row.fidelity = 'reference'; saveEditDraft(image); paint(); }, false, busy(), { class: 'zs-btn quiet small' }) : null),
        el('details', { class: 'zs-details zs-reference-options', open: row.optionsOpen,
          'data-disclosure': `reference-${row.source}-${row.id}`, ontoggle: event => {
            if (event.currentTarget.isConnected) { row.optionsOpen = event.currentTarget.open; saveEditDraft(image); }
          } }, el('summary', {}, '更多设置', el('small', { class: 'zs-reference-state' },
            [use.options[use.selectedIndex].textContent, row.target_selection ? '已选位置' : ''].filter(Boolean).join(' · '))),
          composing && draft.designMode ? null : el('label', { class: 'zs-field' }, el('span', {}, '参考用途'), use), target,
          !composing && single ? button(row.target_selection ? '调整放置位置' : '在底图上选位置', () => choosePart(row, true), false, busy(), { class: 'zs-btn quiet small' }) : null,
          row.target_selection ? button('清除位置', () => { row.target_selection = null; row.fidelity = 'reference'; saveEditDraft(image); paint(); }, false, busy(), { class: 'zs-btn quiet small' }) : null,
          !composing && single && row.use === 'object' ? el('label', { class: 'zs-check' }, original, el('span', {}, '原样贴入照片中的物件')) : null,
          !composing && row.use === 'object' ? el('p', { class: 'zs-help' }, single
            ? '默认参考照片重新绘制。原样贴入会保留照片的光线和视角，可能有拼贴感；需分别选出物件、放置位置和修改范围。'
            : '石材样图按说明重新绘制。要原样贴入照片中的物件，请添加参考照片，再选取物件和放置位置。') : null, name));
    }));
    if (rows.isConnected) { restoreFocus(focus, true); refreshReferenceBudgets(); updateActionButtons(); refreshValidationNotice(); }
  };
  const detect = () => {
    if (materialsLoading || materialsError) return;
    const before = draft.rows.map(row => row.source + ':' + row.id).join('|');
    const named = materials.filter(m => mentionsMaterial(change.value, m, false));
    const single = named.length === 1 && mentionsMaterial(change.value, named[0]) ? named[0] : null;
    suggestion.textContent = named.length > 1 ? '说明里提到多款石材，请点选这次要用的样图。' : '';
    draft.rows = draft.rows.filter(row => !row.auto || row.id === single?.id);
    for (const m of materials) if (m === single && m.available && !draft.suppressed.has(m.id)) {
      const alreadySelected = draft.rows.some(row => row.source === 'material' && row.id === m.id);
      const room = maxEditRows;
      if (!alreadySelected && draft.rows.length >= room)
        suggestion.textContent = '说明里的石材已有样图，但参考图已达上限。请先移除一张，再选用样图。';
      else addMaterial(image, 'material', m.id, m.name, base + 'reference-image/' + m.references[0].id, true);
    }
    saveEditDraft(image);
    if (before !== draft.rows.map(row => row.source + ':' + row.id).join('|')) paint();
  };
  change.addEventListener('input', detect);
  const input = el('input', { type: 'file', class: 'zs-file', accept: '.jpg,.jpeg,.png,.webp', 'aria-label': '添加材质照片', disabled: busy(),
    multiple: true, onchange: event => attachReferences(event.target.files, image).catch(e => tell(e.message, true)) });
  input.setAttribute('aria-label', '添加参考照片');
  const search = el('input', { type: 'search', placeholder: '在资料库找图片', 'aria-label': '搜索参考图片', value: draft.query });
  const results = el('div', { class: 'zs-material-results' });
  const searchNow = async () => {
    const query = search.value.trim(); draft.query = query;
    const id = task.id, epoch = navigation;
    results.replaceChildren(el('p', { class: 'zs-help' }, '正在查找…'));
    try {
      const value = await api('references?q=' + encodeURIComponent(query));
      if (!currentTask(id, epoch) || search.value.trim() !== query || !results.isConnected) return;
      const found = value.references.filter(r => r.kind === 'image');
      results.replaceChildren(...(found.length ? found.map(r => button(r.title, () => {
        addMaterial(image, 'reference', r.id, r.title, base + 'reference-image/' + r.id); paint();
      }, false, busy(), { class: 'zs-material-result' })) : [el('p', { class: 'zs-help' }, '没找到，可以换个名称或上传照片。')]));
    } catch (error) { if (results.isConnected) results.replaceChildren(el('p', { class: 'zs-help' }, error.message)); }
  };
  search.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); void searchNow(); } });
  paint();
  queueMicrotask(() => { if (change.isConnected && change.value) detect(); });
  return el('section', { class: 'zs-materials', 'aria-label': '本次参考图片' },
    el('div', { class: 'zs-section-head' }, el('h3', {}, '参考图片'), el('label', { class: 'zs-btn quiet small zs-material-upload' }, input, '添加照片')),
    stylePacks.length ? button('从精选风格中选用', browseStyles, false, busy(), { class: 'zs-btn quiet small' }) : null,
    materialsLoading ? el('p', { class: 'zs-help', role: 'status' }, '石材样图库正在载入，已选参考仍可查看。')
      : materialsError ? el('p', { class: 'zs-help' }, materialsError) : null,
    el('p', { class: 'zs-help' }, composing && task.interior_input ? '补充照片可选。用途已有默认值，也可以改成材质、家具或其他参考。'
      : composing ? draft.designMode ? '一张也可以。每张图可选材质、造型或风格；只取其中一部分时，点“选取局部”。' : '添加两张或更多照片。可在每张图下写参考内容，也可统一写在组合说明里。' : '可选。一次添加多张照片，当前底图不会变。只需要其中一部分时，点“选取局部”。'),
    image.floorplan_snapshot ? el('p', { class: 'zs-help' }, '本轮还会带上这版绑定的结构灰模，用于核对空间和视角；图片上限已计入它。仍需检查生成结果，不能据效果图量尺寸。') : null,
    el('p', { class: 'zs-help' }, task.job?.status === 'running' ? '已有图片正在生成。这里的设置用于下一次生成，不会改变已完成的图片。'
      : '这里的设置用于下一次生成，不会改变已完成的图片。'),
    editDraftNotice(image),
    referenceBudgetView(image),
    referenceUploadRecovery(image),
    retained, suggestion, rows,
    el('details', { class: 'zs-details' }, el('summary', {}, '从已有图片中选择'),
      task.images.length ? el('div', { class: 'zs-reference-history' }, task.images.map(source => button(
        `${conceptLabel(source.concept_id)} · 版本 ${source.version}`, () => {
          addMaterial(image, 'image', source.id, `${conceptLabel(source.concept_id)} · 版本 ${source.version}`, imageURL(source.id)); paint();
        }, false, busy(), { class: 'zs-btn quiet small' }))) : null,
      (task.edit_assets || []).some(asset => !asset.internal_layout) ? el('div', { class: 'zs-saved-assets' }, el('p', { class: 'zs-help' }, '上传过的照片'),
        task.edit_assets.filter(asset => !asset.internal_layout).map(asset => {
          const label = /^(参考照片|上传材质)\s*\d*$/.test(asset.name || '')
            ? (asset.source_name || '').split(/[\\/]/).at(-1) || '已上传的照片' : asset.name || '已上传的照片';
          const choose = button('', () => {
            addMaterial(image, 'upload', asset.id, asset.name || '参考照片', base + 'asset/' + task.id + '/' + asset.id); paint();
          }, false, busy(), { class: 'zs-btn quiet small zs-saved-asset', 'aria-label': '选用 ' + label, title: label });
          choose.append(el('img', { src: base + 'asset/' + task.id + '/' + asset.id, alt: '', loading: 'lazy' }), el('span', {}, label));
          return choose;
        })) : null),
    el('details', { class: 'zs-details' }, el('summary', {}, '从资料库查找'),
      el('div', { class: 'zs-row' }, search, button('查找', searchNow, false, false)), results,
      materials.length ? el('details', { class: 'zs-details' }, el('summary', {}, '常用石材'), choices) : null));
}
async function savePicture(image, reveal = false) {
  const id = task.id, epoch = navigation;
  await api(`tasks/${id}/${reveal ? 'reveal-image' : 'save-image'}`, { image_id: image.id });
  if (!currentTask(id, epoch)) return;
  const next = await api('tasks/' + id);
  if (!currentTask(id, epoch)) return;
  if (next.revision >= task.revision) task = next;
  render(); tell(reveal ? '已在这台 Mac 的访达中显示。' : '图片已保存到这台 Mac。');
  if (!reveal) panel.querySelector('.zs-notice > span').append(button('打开效果图文件夹', () => savePicture(image, true), false, false, { class: 'zs-btn quiet small' }));
}
async function saveInteriorSet(batch) {
  const owner = task.id, epoch = navigation, key = owner + ':' + batch;
  if (savingInteriorSets.has(key)) return;
  savingInteriorSets.add(key); render();
  try {
    const result = await api(`tasks/${owner}/save-interior-set`, { batch });
    if (!currentTask(owner, epoch)) return;
    const fresh = await api('tasks/' + owner);
    if (!currentTask(owner, epoch)) return;
    if (fresh.revision >= task.revision) task = fresh;
    render(); tell(`已保存 ${result.count} 张室内效果图。` + (result.count < result.total ? '其他视角仍可继续生成。' : ''));
    const concepts = new Set(task.concepts.filter(row => row.batch === batch && row.interior_view).map(row => row.id));
    const saved = task.images.find(row => concepts.has(row.concept_id) && row.exports?.length);
    if (saved) panel.querySelector('.zs-notice > span').append(button('打开效果图文件夹', () => savePicture(saved, true), false, false, { class: 'zs-btn quiet small' }));
  } finally {
    savingInteriorSets.delete(key);
    if (currentTask(owner, epoch)) render();
  }
}
function interiorSetControls(batch, completed, total) {
  if (!completed) return null;
  batch ||= 'legacy';
  const save = button(savingInteriorSets.has(task.id + ':' + batch) ? '正在保存…'
    : status.interior_set_download ? '保存到工作台文件夹'
    : completed === total ? '保存这套图' : `保存已完成的 ${completed} 张`,
    () => saveInteriorSet(batch), false, savingInteriorSets.has(task.id + ':' + batch), { class: 'zs-btn quiet small' });
  if (!status.interior_set_download) return save;
  const more = moreMenu(save);
  more.querySelector('summary').replaceChildren('更多保存方式');
  return el('div', { class: 'zs-row' },
    el('a', { class: 'zs-btn small', href: base + 'interior-set/' + task.id + '/' + batch,
      download: `interior-${task.id.slice(0, 8)}-${batch.slice(0, 8)}.zip` },
      icon('download'), completed === total ? '下载这套图' : `下载已完成的 ${completed} 张`), more);
}
function saveControls(image) {
  return el('div', { class: 'zs-row' },
    button(image.exports?.length ? '已保存' : '保存到效果图文件夹', () => savePicture(image), false, false, { class: 'zs-btn small' }),
    image.exports?.length ? button('在访达中显示', () => savePicture(image, true), false, false, { class: 'zs-btn quiet small' }) : null,
    moreMenu(el('a', { class: 'zs-btn quiet small', href: imageURL(image.id), download: fileName(image),
      onclick: () => tell('已交给浏览器下载，保存位置以浏览器设置为准。通过向日葵操作时，文件在远端 Mac。') }, icon('download'), '浏览器下载'),
      button('存入资料库…', () => { imageSelection.set(task.id, image.id); go(4); }, false, busy(), { class: 'zs-btn quiet small' })));
}
async function useImage(image) {
  if (!task.images.some(row => row.id === image.id)) throw Error('这张图片不在当前设计中。');
  supersedeRouteLoad();
  endComposition();
  imageSelection.set(task.id, image.id); galleryOpen = false; comparing = false; changesOpen = false; render();
}
function endComposition() {
  compositionTask = null;
  try { sessionStorage.removeItem('zyb.studio.compose.' + task.id); } catch {}
}
function copyCompositionReferences(draft, previous) {
  draft.rows = (previous?.rows || []).map(row => ({ ...structuredClone(row), target: '', region: null,
    target_selection: null, fidelity: 'reference', optionsOpen: false, auto: false,
    use: draft.designMode || ['reference', 'object', 'shape', 'material', 'pattern', 'style'].includes(row.use) ? row.use : 'reference' }));
  draft.suppressed = new Set(previous?.suppressed || []);
  draft.referencesTouched = true;
  saveEditDraft({ id: 'compose' });
}
function reuseImageReferences() {
  const selected = imageSelection.get(task.id);
  if (!task.images.some(image => image.id === selected)) throw Error('请先选一张图片，确认它的参考设置。');
  const previous = currentEdit({ id: selected }), draft = currentEdit({ id: 'compose' });
  if (draft.rows.length && !window.confirm(`将组合草稿中的 ${draft.rows.length} 份参考替换为当前图的 ${previous.rows.length} 份参考？原图草稿和历史图片不变，放置范围需要重新选择。`)) return;
  copyCompositionReferences(draft, previous);
  render();
  tell(`已沿用当前图的 ${draft.rows.length} 份参考。选用范围保留，放置范围需要重新选择。`);
}
function beginComposition(designMode = false) {
  const key = task.id + ':compose';
  let saved = editDrafts.has(key);
  if (!saved) try { saved = Boolean(JSON.parse(sessionStorage.getItem('zyb.studio.edits.' + key) || 'null')); } catch {}
  const draft = currentEdit({ id: 'compose' });
  draft.designMode = designMode === true || Boolean(task.interior_input);
  const selected = imageSelection.get(task.id);
  if (!saved && !draft.designMode && task.images.some(image => image.id === selected)) {
    copyCompositionReferences(draft, currentEdit({ id: selected }));
  }
  if (!draft.designMode) for (const row of draft.rows) {
    if (!['reference', 'object', 'shape', 'material', 'pattern', 'style'].includes(row.use)) row.use = 'reference';
  }
  saveEditDraft({ id: 'compose' });
  supersedeRouteLoad();
  compositionTask = task.id; galleryOpen = false; comparing = false; changesOpen = false;
  try { sessionStorage.setItem('zyb.studio.compose.' + task.id, '1'); } catch {}
  stage = 3; routePush = true; render();
}
function receiveReferences(image) {
  const receive = event => {
    const files = [...(event.clipboardData?.files || event.dataTransfer?.files || [])];
    if (!files.length || event.defaultPrevented || event.target.closest('.zs-more-upload')) return;
    event.preventDefault();
    if (busy()) return tell('当前正在处理，完成后再添加照片。', true);
    attachReferences(files, image).catch(error => tell(error.message, true));
  };
  view.addEventListener('paste', receive);
  view.addEventListener('dragover', event => { if (event.dataTransfer?.types.includes('Files')) event.preventDefault(); });
  view.addEventListener('drop', receive);
}
function floorplanIssue(draft) {
  if (!task.floorplan || !draft.designMode) return '';
  const viewIssue = floorplanViewIssue(task.floorplan.spec);
  if (viewIssue) return viewIssue;
  const valid = task.floorplan.guides.every(guide => draft.rows.some(row => row.source === 'upload' && row.id === guide.id &&
    row.use === 'shape' && !row.source_selection));
  return valid ? '' : '平面复核图、结构灰模和深度底稿都需要完整参考，并将用途设为造型。可点“恢复结构底稿”。';
}
function restoreFloorplanGuides() {
  const image = { id: 'compose' }, draft = currentEdit(image);
  const ids = new Set(task.floorplan.guides.map(row => row.id));
  const priorGuideIds = new Set([
    ...ids,
    ...(task.floorplan_history || []).flatMap(record => (record.guides || []).map(row => row.id)),
    ...(task.edit_assets || []).filter(row => ['plan', 'structure', 'depth'].includes(row.guide_kind)).map(row => row.id),
  ]);
  const kept = draft.rows.filter(row => !(row.source === 'upload' && priorGuideIds.has(row.id)));
  if (kept.length + ids.size > maxEditRows) throw Error('请先移除不用的参考卡片，再恢复三张结构底稿。');
  draft.rows = [...task.floorplan.guides.map(guide => ({ source: 'upload', id: guide.id, name: guide.name,
    preview: base + 'asset/' + task.id + '/' + guide.id, use: 'shape', fidelity: 'reference',
    take: '保持已确认的墙体、开口、功能区与相机视角，只调整材质和光线。' })), ...kept];
  draft.referencesTouched = true; saveEditDraft(image); render();
}
function interiorGenerationGoal(value, interior = false) {
  return value.trim() || (interior ? '依据原始平面图，设计整套照片级室内效果，分别呈现各个空间的人眼高度广角视角。' : '');
}
function interiorRoomGroups(concepts) {
  const rooms = new Map();
  for (const [index, concept] of concepts.entries()) {
    const binding = concept.interior_view;
    if (!binding) continue;
    const key = `${binding.layout_id || ''}:${binding.layout_sha256 || ''}:${binding.space_id}`;
    if (!rooms.has(key)) rooms.set(key, { id: binding.space_id,
      label: binding.space_label || concept.name.split(' · ')[0], indices: [] });
    rooms.get(key).indices.push(index);
  }
  return [...rooms.values()];
}
function interiorViewBatches(concepts, images = []) {
  const batches = new Map();
  for (const concept of concepts) {
    if (!concept.interior_view) continue;
    const id = concept.batch || 'legacy';
    if (!batches.has(id)) batches.set(id, []);
    batches.get(id).push(concept);
  }
  return [...batches].map(([id, rows], index) => ({ id, round: index + 1, concepts: rows,
    style: images.findLast(image => image.concept_id === rows[0].id)?.floorplan_input?.style_label
      || rows[0].interior_style_label || '室内设计' })).reverse();
}
function resumeInteriorButton(batchId) {
  if (!status.interior_batch_resume) return null;
  const progress = interiorBatchProgress(task, batchId);
  if (!progress.resumable.length) return null;
  const originalStyle = progress.style && progress.style !== task.interior_input?.style_label;
  const label = originalStyle ? `沿用${progress.style}补齐（${progress.resumable.length} 张）`
    : `补齐这套图（${progress.resumable.length} 张）`;
  return quiet(paidButton(label, authorization => action('design', { ...authorization, resume_batch_id: batchId })));
}
function generateComposition(image, value, authorization = {}) {
  if (pendingUploads.has(task.id + ':references')) throw Error('参考照片还在处理，请稍后再生成。');
  const draft = currentEdit(image), designing = draft.designMode;
  const interior = designing && Boolean(task.interior_input);
  const edits = selectedEdits(image), text = interiorGenerationGoal(value, interior);
  const interiorProblem = interior ? interiorIssue(task.interior_input, edits) : '';
  if (interiorProblem) throw Error(interiorProblem);
  if (edits.length < (designing ? 1 : 2)) throw Error(designing ? '请添加参考图片。' : '请至少添加两张参考图。');
  if (!text) throw Error('请说明各张图取用什么、想怎样组合。');
  const issue = referenceBudgetIssue(image);
  if (issue) throw Error(issue);
  return action(designing ? 'design' : 'compose', { ...getSettings(), ...authorization, change: text, edits,
    ...(designing ? { count: draft.count || (interior ? 1 : 3) } : {}) });
}
function compositionView() {
  const image = { id: 'compose' }, draft = currentEdit(image), designing = draft.designMode;
  const interior = designing && Boolean(task.interior_input);
  const coverage = interior && status.interior_coverage;
  const change = field('change', designing ? task.brief.goal : '', null, interior ? '想要的效果' : designing ? '想设计什么？' : '想怎样组合？');
  const composedLatest = task.images.filter(image => image.kind === 'compose').at(-1);
  change.querySelector('textarea').placeholder = interior ? '例如：温暖的浅木色、自然光，家具位置沿用平面图。'
    : designing ? '例如：参考图 1 的石材用于地面，参考图 2 的木作风格用于墙面，设计一个新空间。' : '例如：用参考图 1 的主体、参考图 2 的底座，组合成一件完整作品。';
  const quantity = segmented('design_count', String(draft.count || (interior ? 1 : 3)), [['1', '1 个'], ['2', '2 个'], ['3', '3 个']], '生成几个方案？');
  for (const radio of quantity.querySelectorAll('input')) radio.dataset.draft = 'off';
  quantity.addEventListener('change', () => { draft.count = Number(quantity.querySelector('input:checked').value); saveEditDraft(image); });
  const structureAspect = designing && task.floorplan ? '4:3' : null;
  const { box, summary } = settings(structureAspect || (interior ? '4:3' : '1:1'), null, structureAspect);
  receiveReferences(image);
  const results = task.concepts.filter(concept => concept.reference_design);
  const currentBatch = results.at(-1)?.batch;
  const covered = results.filter(concept => concept.batch === currentBatch && concept.interior_view);
  const completedViews = covered.filter(concept => latest(concept.id)).length;
  const completedRooms = new Set(covered.filter(concept => latest(concept.id)).map(concept => concept.interior_view.space_id)).size;
  const batchResults = results.filter(concept => concept.batch === currentBatch);
  const resultCards = batchResults.map(concept => {
    const result = latest(concept.id), state = cardState(concept.id), waiting = ['running', 'queued'].includes(state), pending = state === 'registration-pending';
    const oldStyle = result?.floorplan_input?.style_label || concept.interior_style_label;
    const retryLabel = interior && oldStyle && oldStyle !== task.interior_input.style_label
      ? `沿用${oldStyle}再出这张` : result ? '按这版要求再出一张' : '重新出这张';
    return el('article', { class: 'zs-reference-result' }, result ? el('a', {
      class: 'zs-photo-preview', href: imageURL(result.id), target: '_blank', rel: 'noopener',
      onclick: event => {
        if (coverage) inspectInteriorPhoto(event, batchResults, result);
      },
      'aria-label': '放大查看' + conceptLabel(concept.id) },
      el('img', { src: imageURL(result.id), alt: conceptLabel(concept.id), loading: 'lazy', width: result.width, height: result.height }))
      : el('p', { role: 'status', class: 'zs-help' }, waiting ? '正在生成…' : pending ? '图片已保存，可以恢复到这份设计' : state === 'uncertain' ? '这张的结果还未确认，其他已完成图片仍可查看和保存' : '这张还没有生成'),
      el('strong', {}, conceptLabel(concept.id)),
      result && pending ? el('p', { role: 'status', class: 'zs-help' }, '新图已保存，待登记；当前显示之前的图') : null,
      result ? button('在这张上改', () => useImage(result), false, uploading || planning, { class: 'zs-btn quiet small' }) : null,
      !waiting && !pending && state !== 'uncertain' ? quiet(paidButton(retryLabel, authorization =>
        action('design', { ...authorization, retry_concept_id: concept.id }))) : null);
  });
  return [el('div', { class: 'zs-section-head' }, heading(interior ? '平面图生成室内实景' : designing ? '参考图片做新方案' : '用多张图组合',
    interior ? '按平面图的空间布局，结合所选风格和补充照片，生成写实室内效果。' : designing ? '选择每张图的用途，再生成独立的效果图。' : '取用各张图片中的内容，生成一个新方案。'),
    el('div', { class: 'zs-row' }, interior ? button('换个风格', changeInteriorStyle, false, busy(), { class: 'zs-btn quiet small' }) : null,
      button(task.images.length ? '返回原方案' : '返回设计', () => { supersedeRouteLoad(); endComposition(); if (!task.images.length) stage = 1; render(); }, false, busy()))),
    !designing && task.images.some(row => row.id === imageSelection.get(task.id)) ? el('div', { class: 'zs-row' },
      el('p', { class: 'zs-help' }, '组合草稿单独保留。可重新沿用当前图的参考；替换前会询问。'),
      button('沿用当前图的参考…', reuseImageReferences, false, busy(), { class: 'zs-btn quiet small' })) : null,
    floorplanBasis(designing ? task.floorplan : null),
    interiorBasis(interior ? task.interior_input : null),
    interior && interiorIssue(task.interior_input, draft.rows) ? el('div', { class: 'zs-row' },
      el('p', { class: 'zs-help' }, interiorIssue(task.interior_input, draft.rows)),
      button('恢复原始平面图', () => {
        const original = task.interior_input.edits.find(row => row.source === 'upload' && row.id === task.interior_input.source_id);
        const kept = draft.rows.filter(row => !(row.source === 'upload' && row.id === task.interior_input.source_id));
        if (!original || kept.length >= maxEditRows) throw Error('请先移除不用的参考，再恢复原始平面图。');
        draft.rows = [{ ...original, preview: base + 'asset/' + task.id + '/' + original.id }, ...kept];
        saveEditDraft(image); render();
      }, false, busy(), { class: 'zs-btn quiet small' })) : null,
    task.references.some(ref => !draft.rows.some(row => row.source === 'reference' && row.id === ref.id))
      ? el('p', { class: 'zs-help' }, '部分已选资料只保存在设计记录中，不参与这次图片生成；实际参考以图片卡片为准。') : null,
    designing && task.floorplan ? el('div', { class: 'zs-row' },
      el('p', { class: 'zs-help' }, '三张底稿完整参与参考；它们不是结构约束模型，结果仍要逐项核对。'),
      button('恢复结构底稿', restoreFloorplanGuides, false, busy(), { class: 'zs-btn quiet small' })) : null,
    designing && results.length ? el('section', { class: 'zs-reference-results', 'aria-label': coverage ? '本轮室内视角' : '本轮参考方案', style: '--result-count:' + (coverage ? 1 : Math.min(3, resultCards.length)) },
      coverage && covered.length ? el('div', { class: 'zs-row', style: 'grid-column: 1 / -1' },
        el('p', { class: 'zs-help' }, `本轮 ${new Set(covered.map(row => row.interior_view.space_id)).size} 个空间 · 已呈现 ${completedRooms} 个 · 已生成 ${completedViews}/${covered.length} 个视角`),
        resumeInteriorButton(currentBatch),
        interiorSetControls(currentBatch, completedViews, covered.length)) : null,
      coverage ? [interiorRoundOverview(batchResults),
        ...interiorRoomGroups(batchResults).map(room => interiorRoomSection(room, batchResults, resultCards))] : resultCards,
      button(coverage ? '查看全部视角' : '查看全部方案', () => go(2), false, false, { class: 'zs-btn quiet small' })) : null,
    !designing && composedLatest ? el('section', { class: 'zs-composition-result', 'aria-label': '最近生成的组合' },
      el('img', { src: imageURL(composedLatest.id), alt: '最近生成的组合方案' }),
      el('div', {}, el('h3', {}, '最近生成的组合'), el('p', {}, `${conceptLabel(composedLatest.concept_id)} · 版本 ${composedLatest.version} · ${composedLatest.width} × ${composedLatest.height}`),
        button('打开这张图', () => useImage(composedLatest), false, uploading || planning, { class: 'zs-btn quiet small' }))) : null,
    el('div', { class: 'zs-composition-workspace' },
      el('section', { class: 'zs-composition-references' }, materialEditor(image, change.querySelector('textarea'))),
      el('aside', { class: 'zs-editor' }, change,
        el('p', { class: 'zs-help' }, coverage ? '自动识别图中的空间，分别生成不同视角。出图后可以逐张调整，之前的图片会保留。'
          : interior ? '按平面图布置空间，结合所选风格出图。生成后可以继续调整，之前的图片会保留。'
          : designing && task.floorplan ? '结构灰模用作主底图，平面与深度底稿辅助核对，风格图只提供材质和光线。出图后仍需复核；原图、底稿和每轮结果都会保留。' : '会重新设计整体布局。若要保持某张图的其他部分不变，请返回那张图使用局部修改。'),
        designing && !coverage ? quantity : null,
        el('details', { class: 'zs-output-options' }, el('summary', {}, '图片尺寸和比例', summary), box), blocker(),
        el('div', { class: 'zs-editor-actions' }, generationControl(paidButton(interior ? '生成室内实景' : designing ? '生成方案' : '生成组合方案', authorization =>
          generateComposition(image, change.querySelector('textarea').value, authorization)), () => pendingUploads.has(task.id + ':references') ? '参考资料或照片尚未处理完，请继续处理或取消剩余内容。'
          : interior && interiorIssue(task.interior_input, currentEdit(image).rows) ? interiorIssue(task.interior_input, currentEdit(image).rows)
          : designing && floorplanIssue(currentEdit(image)) ? floorplanIssue(currentEdit(image))
          : currentEdit(image).rows.length < (designing ? 1 : 2) ? designing ? '添加至少一张参考照片。' : '再添加参考照片，至少需要两张。'
          : designing && currentEdit(image).rows.some(row => !row.use) ? '请选择每张参考图的用途。'
          : !interior && !change.querySelector('textarea').value.trim() ? '写下想怎样组合这些照片。' : referenceBudgetIssue(image)),
          coverage ? moreMenu(quiet(paidButton('重新识别空间并生成', authorization => {
            const edits = selectedEdits(image), text = interiorGenerationGoal(change.querySelector('textarea').value, true);
            return action('design', { ...getSettings(), ...authorization, change: text, edits, count: 1, refresh_layout: true });
          }))) : null) ))];
}
function localEditControls(image) {
  const draft = currentEdit(image);
  const choose = () => {
    const owner = task.id, epoch = navigation;
    openSelection({ url: imageURL(image.id), initial: draft.selection, title: '选择要修改的范围', reference: { source: 'image', id: image.id },
      owner, api, segmentReady: status.segment_ready, intent: 'edit',
      commit: (selection, proof) => {
        if (!currentTask(owner, epoch) || imageSelection.get(owner) !== image.id) return;
        draft.selection = selection; draft.selection_proof = proof; draft.mode = 'local'; saveEditDraft(image); render();
      } });
  };
  const mode = segmented('edit_scope', draft.mode || 'full', [['full', '整体调整'], ['local', '只改选区']], '修改范围');
  for (const radio of mode.querySelectorAll('input')) radio.dataset.draft = 'off';
  mode.addEventListener('change', () => { draft.mode = mode.querySelector('input:checked').value; saveEditDraft(image); render(); });
  return el('section', { class: 'zs-local-controls' }, mode,
    draft.mode === 'local' ? button(draft.selection ? '调整修改范围' : '选择修改范围', choose, false, busy(), { class: 'zs-btn quiet small' }) : null,
    draft.mode === 'local' && draft.selection ? button('清除范围', () => { draft.selection = null; draft.selection_proof = null; saveEditDraft(image); render(); }, false, busy(), { class: 'zs-btn quiet small' }) : null,
    el('p', { class: 'zs-help' }, draft.mode === 'local'
      ? draft.selection ? '只修改蓝色范围，其余保留原图。替换物件时，请把新物件和阴影所需的空间也选进去。' : '在图片上选出要改的地方，其余保留原图。请给新物件和阴影留出空间。'
      : '整张图参与生成，未点名的细节也可能变化。需要保留其他部分时，选“只改选区”。'));
}
function editIssue(image, text) {
  if (pendingUploads.get(task.id + ':references')?.imageId === image.id) return '参考资料或照片尚未处理完，请继续处理或取消剩余内容。';
  const viewIssue = image.floorplan_snapshot ? floorplanViewIssue(image.floorplan_snapshot.spec) : '';
  if (viewIssue) return viewIssue;
  if (!text.trim()) return '写下想修改的内容。';
  const draft = currentEdit(image);
  const budgetIssue = referenceBudgetIssue(image);
  if (budgetIssue) return budgetIssue;
  if (draft.mode === 'local' && (!draft.selection || !draft.selection_proof)) return '选择修改范围后，就可以生成。';
  for (const [i, row] of draft.rows.entries()) {
    if (row.fidelity !== 'original') continue;
    if (draft.mode !== 'local') return '原样贴入需要切换到“只改选区”。';
    if (!row.source_selection) return `先在参考图 ${i + 1} 中选出要贴入的物件。`;
    if (!row.target_selection) return `在参考图 ${i + 1} 的“更多设置”中选择放置位置。`;
  }
  return '';
}
function editOptions(image, { includeReferences = true } = {}) {
  if (pendingUploads.get(task.id + ':references')?.imageId === image.id) throw Error('参考资料或照片尚未处理完，请继续处理或取消剩余内容。');
  const viewIssue = image.floorplan_snapshot ? floorplanViewIssue(image.floorplan_snapshot.spec) : '';
  if (viewIssue) throw Error(viewIssue);
  const draft = currentEdit(image);
  const budgetIssue = includeReferences ? referenceBudgetIssue(image) : '';
  if (budgetIssue) throw Error(budgetIssue);
  if (draft.mode === 'local' && (!draft.selection || !draft.selection_proof)) throw Error('请先选择并确认修改范围。');
  return { edits: selectedEdits(image), edit_mode: draft.mode || 'full', selection: draft.selection || null,
    selection_proof: draft.selection_proof || null };
}
function allImagesView() {
  return [el('div', { class: 'zs-section-head' }, heading('全部图片', '点选相中的那一版，再继续修改。'),
    cloudImages() ? button('用多张图组合', beginComposition, false, busy()) : null,
    imageSelection.has(task.id) ? button('回到当前图片', () => { supersedeRouteLoad(); galleryOpen = false; render(); }, false, false) : null),
    ...task.concepts.map(c => el('section', { class: 'zs-image-group' }, el('h3', {}, c.interior_photographic ? conceptLabel(c.id) : `${conceptLabel(c.id)} · ${c.name}`),
      el('div', { class: 'zs-image-gallery' }, task.images.filter(i => i.concept_id === c.id).map(image => el('article', { class: 'zs-image-card' },
        el('img', { src: imageURL(image.id), alt: `${conceptLabel(c.id)}，版本 ${image.version}`, loading: 'lazy', width: image.width, height: image.height }),
        el('strong', {}, `版本 ${image.version}${image.exports?.length ? ' · 已保存' : ''}${task.archives.some(a => a.image_id === image.id) ? ' · 已存档' : ''}`),
        el('p', { class: 'zs-help' }, versionNote(image, task.images)),
        image.edits?.length ? el('p', { class: 'zs-help' }, image.edits.map(e => e.name).join(' / ')) : null,
        button('在这张上改', () => useImage(image), false, uploading || planning))))))];
}
async function showChanges(container, before, after, owner) {
  if (!container.isConnected) return;
  const explanation = el('p', { class: 'zs-diff-label', role: 'status' }, '正在比对两版图片…');
  container.append(explanation);
  try {
    if (Math.abs((before.width / before.height) / (after.width / after.height) - 1) > .025)
      throw Error('两版比例不同，请分别放大查看。');
    const load = async image => { const picture = new Image(); picture.src = imageURL(image.id, owner); await picture.decode(); return picture; };
    const [original, result] = await Promise.all([load(before), load(after)]);
    if (!container.isConnected) return;
    const scale = Math.min(1, 1400 / Math.max(result.naturalWidth, result.naturalHeight));
    const width = Math.round(result.naturalWidth * scale), height = Math.round(result.naturalHeight * scale);
    const first = el('canvas', { width, height }), overlay = el('canvas', { width, height, class: 'zs-diff', role: 'img',
      'aria-label': '变化区域对比，红色标出较大色差' });
    const oldContext = first.getContext('2d', { willReadFrequently: true }), newContext = overlay.getContext('2d', { willReadFrequently: true });
    oldContext.filter = newContext.filter = 'blur(1.5px)';
    oldContext.drawImage(original, 0, 0, width, height); newContext.drawImage(result, 0, 0, width, height);
    const old = oldContext.getImageData(0, 0, width, height).data, next = newContext.getImageData(0, 0, width, height).data;
    const changed = new Uint8Array(width * height);
    for (let p = 0; p < changed.length; p++) {
      const i = p * 4;
      changed[p] = Math.max(Math.abs(old[i] - next[i]), Math.abs(old[i + 1] - next[i + 1]), Math.abs(old[i + 2] - next[i + 2])) > 36 ? 1 : 0;
    }
    newContext.filter = 'none'; newContext.drawImage(result, 0, 0, width, height);
    const marked = newContext.getImageData(0, 0, width, height);
    for (let y = 1; y < height - 1; y++) for (let x = 1; x < width - 1; x++) {
      const p = y * width + x, i = p * 4;
      const neighbors = changed[p - 1] + changed[p + 1] + changed[p - width] + changed[p + width];
      if (changed[p] && neighbors >= 3) {
        marked.data[i] = Math.round(marked.data[i] * .3 + 255 * .7);
        marked.data[i + 1] = Math.round(marked.data[i + 1] * .3);
        marked.data[i + 2] = Math.round(marked.data[i + 2] * .3);
      }
    }
    newContext.putImageData(marked, 0, 0);
    container.querySelector('[data-main-preview]')?.replaceWith(overlay);
    explanation.textContent = '红色只表示较大色差，不是修改范围。未标红也可能已改变，面部和小像仍须放大核对。原文件未改变。';
  } catch (error) { if (container.isConnected) explanation.textContent = error.message === '两版比例不同，请分别放大查看。' ? error.message : '暂时无法比对，请分别放大查看。'; }
}
async function refreshDimensionExport(result, owner, epoch) {
  try {
    const fresh = await api('tasks/' + owner);
    if (currentTask(owner, epoch) && fresh.revision >= task.revision) { task = fresh; render(); }
  } catch { return { ...result, refresh_failed: true }; }
  return result;
}
function refineView() {
  if (compositionTask === task.id || (!task.images.length && task.edit_assets?.length && !task.selected)) return compositionView();
  if (galleryOpen) return allImagesView();
  const selectedEmpty = task.concepts.some(c => c.id === task.selected) && !task.images.some(i => i.concept_id === task.selected);
  const chosen = task.images.find(i => i.id === imageSelection.get(task.id)) || (!selectedEmpty && task.images.length === 1 ? task.images[0] : null);
  if (!chosen && task.images.length > 1 && !selectedEmpty) return allImagesView();
  const concept = task.concepts.find(c => c.id === (chosen?.concept_id || task.selected));
  if (!concept) return [empty('还没有选方案', '先挑一个方案，再在这里生成和修改图片。', button('去挑方案', () => { supersedeRouteLoad(); stage = 2; render(); }, true, false),
    task.images.length ? button('查看全部图片', () => { supersedeRouteLoad(); galleryOpen = true; render(); }, false, false) : null)];
  const images = task.images.filter(i => i.concept_id === concept.id);
  const active = images.find(i => i.id === imageSelection.get(task.id)) || images.at(-1);
  const pending = !active && cardState(concept.id) === 'registration-pending';
  activeImageId = active?.id || null;
  if (active) {
    imageSelection.set(task.id, active.id);
    try { sessionStorage.setItem('zyb.studio.base.' + task.id, active.id); } catch {}
  }
  const index = images.indexOf(active);
  const parent = active && task.images.find(i => i.id === active.parent_id);
  if (!parent) { comparing = false; changesOpen = false; }
  const shown = comparing ? parent : active;
  const pick = id => { supersedeRouteLoad(); imageSelection.set(task.id, id); comparing = false; changesOpen = false; render(); };
  const canvas = el('section', { class: 'zs-canvas' + (comparing ? ' comparing' : ''), 'aria-label': '当前图片' },
    active ? el('img', { src: imageURL(shown.id), alt: `${concept.name}，版本 ${shown.version}`, width: shown.width, height: shown.height,
      'data-main-preview': '1', fetchpriority: 'high' })
      : el('div', { class: 'zs-canvas-empty' }, icon('photo'), el('strong', {}, pending ? '图片已保存，待登记' : '这个方案还没有图片'),
        el('p', {}, pending ? '请先恢复已有结果，再查看或修改。' : '点击「生成效果图」开始。')),
    comparing ? el('span', { class: 'zs-canvas-tag' }, `修改前：版本 ${parent.version}`) : null,
    index > 0 ? el('button', { class: 'zs-nav prev', type: 'button', 'aria-label': '上一版', 'data-focus': 'prev', onclick: () => pick(images[index - 1].id) }, icon('back')) : null,
    index >= 0 && index < images.length - 1 ? el('button', { class: 'zs-nav next', type: 'button', 'aria-label': '下一版', 'data-focus': 'next', onclick: () => pick(images[index + 1].id) }, icon('next')) : null);
  const caption = active ? el('div', { class: 'zs-caption' },
    el('p', { 'data-version-details': '1' }, el('strong', {}, `版本 ${shown.version}`), el('span', {}, versionNote(shown, images)),
      el('span', { class: 'zs-num' }, `${shown.width} × ${shown.height}`)),
    el('div', { class: 'zs-row' },
      parent ? el('button', { class: 'zs-btn quiet small', type: 'button', 'aria-pressed': String(comparing), 'data-focus': 'compare',
        onclick: () => { comparing = !comparing; changesOpen = false; render(); } }, icon('compare'), comparing ? '看修改后' : '对比修改前') : null,
      parent ? button(changesOpen ? '关闭色差标记' : '标出明显色差', () => { changesOpen = !changesOpen; comparing = false; render(); }, false, false,
        { class: 'zs-btn quiet small', 'aria-pressed': String(changesOpen) }) : null,
      parent ? el('a', { class: 'zs-btn quiet small', href: imageURL(parent.id), target: '_blank', rel: 'noopener' }, '修改前原图') : null,
      el('a', { class: 'zs-btn quiet small', href: imageURL(active.id), target: '_blank', rel: 'noopener' }, parent ? '修改后原图' : '放大查看原图')),
    floorplanBasis(shown.floorplan_snapshot || null),
    interiorBasis(shown.floorplan_input?.mode === 'floorplan_photographic' ? shown.floorplan_input : null),
    el('div', { class: 'zs-row' }, saveControls(shown), shown.floorplan_input?.mode === 'floorplan_photographic' && !shown.dimensions?.items?.length ? null : button(shown.dimensions?.items?.length ? '查看尺寸标注' : '标注已知尺寸', () => {
      const owner = task.id, epoch = navigation;
      return openDimensions({ owner, image: shown, url: imageURL(shown.id, owner),
        isCurrent: () => currentTask(owner, epoch),
        load: async () => {
          const fresh = await api('tasks/' + owner);
          if (!currentTask(owner, epoch)) throw Error('设计已切换，请重新打开标注。');
          if (fresh.revision >= task.revision) task = fresh;
          return api('dimensions/' + owner + '/' + shown.id);
        },
        save: async values => {
          if (!currentTask(owner, epoch)) return false;
          try { return await action('dimensions', values); }
          catch (error) {
            if (!error.submissionUncertain) throw error;
            try {
              const snapshot = await api('dimensions/' + owner + '/' + shown.id);
              const fresh = await api('tasks/' + owner);
              if (!currentTask(owner, epoch)) return false;
              if (fresh.revision >= task.revision) { task = fresh; render(); schedule(); }
              if (snapshot.document?.source_sha256 === values.source_sha256 && dimensionItemsMatch(snapshot.document.items, values.items))
                return snapshot.document;
            } catch {}
            const unresolved = Error('标注提交结果尚未确认。未重复保存，请关闭并重新打开，核对最新记录；本标签页草稿保留。');
            unresolved.submissionUncertain = true; throw unresolved;
          }
        },
        exportSheet: async values => {
          if (!currentTask(owner, epoch)) throw Error('设计已切换，请重新打开标注。');
          let result;
          const title = task.brief.title;
          try { result = await api('tasks/' + owner + '/dimension-export', { revision: task.revision, image_id: shown.id, ...values }); }
          catch (error) {
            if (error.submissionUncertain) {
              try {
                const snapshot = await api('dimensions/' + owner + '/' + shown.id);
                for (const record of (snapshot.exports || []).slice().reverse()) {
                  if (!record.url || !record.metadata_url?.startsWith(base + 'dimension-file/' + owner + '/' + shown.id + '/')) continue;
                  try {
                    const receipt = await api(record.metadata_url.slice(base.length));
                    if (receipt.schema === 2 && receipt.layout_version === 3 && receipt.kind === 'known_dimensions'
                      && receipt.task_id === owner && receipt.image_id === shown.id && receipt.version === shown.version && receipt.title === title
                      && receipt.source_sha256 === values.expected_source_sha256 && dimensionItemsMatch(receipt.items, values.expected_items)) {
                      result = { saved: true, recovered: true, url: record.url, metadata_url: record.metadata_url }; break;
                    }
                  } catch {}
                }
              } catch {}
              if (!result) {
                const unresolved = Error('导出结果尚未确认。未重复导出，请关闭并重新打开，核对已导出记录。');
                unresolved.submissionUncertain = true; throw unresolved;
              }
            }
            else {
            if (/已更新，请刷新后重试|另一窗口更新/.test(error.message))
              throw Error('这份设计已被另一窗口更新。未导出，请关闭标注并重新打开，核对后再导出。');
            throw error;
            }
          }
          return refreshDimensionExport(result, owner, epoch);
        } });
    }, false, busy(), { class: 'zs-btn quiet small' }))) : null;
  const original = active?.kind === 'upload';
  const change = field('change', '', null, !active ? '' : original ? '想把这张照片改成什么样？' : '在这一版上还想改什么？');
  change.querySelector('textarea').placeholder = active && currentEdit(active).mode === 'local'
    ? '例如：把选中的底座改成哑光黑色。也可以添加照片，说明想参考哪张图的哪部分。'
    : original ? '例如：去掉照片里的人，在正面墙前放一尊坐姿铜佛像，调整周围陈设。' : '例如：把底座改成哑光黑色，整体采用柔和的暖色光线。';
  const { box, summary } = settings(active && cloudImages() ? 'source' : active?.aspect || '1:1', active?.size);
  const compareTarget = field('variant_target', '', null, '比较部位');
  compareTarget.querySelector('input').placeholder = '例如：佛像正后方中间的一块墙板';
  compareTarget.querySelector('input').maxLength = 40;
  const generate = active ? paidButton(original ? '生成效果图' : '生成修改版', authorization => {
    imageSelection.set(task.id, active.id);
    return action('refine', { ...getSettings(), ...authorization, image_id: active.id, change: change.querySelector('textarea').value,
      ...editOptions(active) });
  }, 'image', comparing || changesOpen) : concept.reference_design
    ? paidButton('生成效果图', authorization => action('design', { ...authorization, retry_concept_id: concept.id }))
    : concept.composition ? button('调整组合参考', beginComposition, true, busy())
      : paidButton('生成效果图', authorization => action('generate', { ...getSettings(), ...authorization, concept_ids: [concept.id] }));
  const editor = el('aside', { class: 'zs-editor', 'aria-label': '修改图片' },
    active ? el('h3', { class: 'zs-base' }, `在 ${conceptLabel(concept.id)} · 版本 ${active.version} 上改`)
      : el('p', { class: 'zs-base' }, pending ? '已有图片待登记，请使用上方的“恢复已有结果”。' : `按“${concept.name}”生成第一张效果图。`),
    button(`换底图 · 全部 ${task.images.length} 张`, () => { supersedeRouteLoad(); galleryOpen = true; render(); }, false, false, { class: 'zs-btn quiet small' }),
    task.interior_input && cloudImages() ? button('换个风格', changeInteriorStyle, false, busy(), { class: 'zs-btn quiet small' }) : null,
    cloudImages() ? button(task.interior_input ? '按平面图再出室内实景' : '用多张图组合新方案', beginComposition, false, busy(), { class: 'zs-btn quiet small' }) : null,
    comparing ? button('用修改前这张继续', () => pick(parent.id), false, false) : null,
    active ? change : null,
    active && comparing || changesOpen ? el('p', { class: 'zs-help' }, '当前在对比图片，退出对比后可生成。') : null,
    active && cloudImages() ? localEditControls(active) : null,
    active?.local_edit?.outside_changed_pixels === 0 ? el('p', { class: 'zs-help' }, '这张图的选区外像素与底图一致。内容与工艺仍待核对。') : null,
    active?.local_edit?.edge_residual > 20 ? el('p', { class: 'zs-help' }, '选区边缘的变化较大，请放大检查接缝与光影。') : null,
    active?.local_edit?.patch_upscale > 1.3 ? el('p', { class: 'zs-help' }, '修改部分经过放大，请检查细节清晰度。可缩小修改范围或选更大尺寸。') : null,
    active && cloudImages() ? materialEditor(active, change.querySelector('textarea')) : null,
    el('details', { class: 'zs-output-options' }, el('summary', {}, '图片尺寸和比例', summary), box),
    blocker(),
    active && cloudImages() && materials.filter(m => m.available).length > 1 ? el('details', { class: 'zs-details' }, el('summary', {}, '比较几款石材'),
      el('p', { class: 'zs-help' }, '从同一底图比较这个部位的材质，不沿用上面的其他修改说明。比较沿用当前修改范围；只改选区时，请先确认选区就是要比较的部位。'), compareTarget,
      quiet(paidButton(`主推石材各出一张（${Math.min(3, materials.filter(m => m.available).length)} 张）`, authorization => {
      const target = compareTarget.querySelector('input').value.trim();
      if (!target) throw Error('请填写比较部位。');
      if (materials.some(m => mentionsMaterial(target, m, false)) || /不|别|除了|去掉|移除|取消|换成|改成/.test(target))
        throw Error('这里只写部位，例如：佛像后面的墙板。');
      const instruction = `将图1的${target}替换为本次材质样图所示石材，其他部分不变。`;
      return action('refine', { ...getSettings(), ...authorization, image_id: active.id, change: instruction, variant_target: target,
        ...editOptions(active, { includeReferences: false }), edits: [],
        variants: materials.filter(m => m.available).slice(0, 3).map(m => [{ source: 'material', id: m.id }]) });
    }, 'image', comparing || changesOpen))) : null,
    !cloudImages() && active ? button('只导出这一版的高清图', () => action('export', { ...getSettings(), image_id: active.id })) : null,
    active ? el('section', { class: 'zs-vision', 'aria-label': '自动检查' }, visionView(active)) : null,
    el('details', { class: 'zs-details zs-more-upload' }, el('summary', {}, '以新照片另起一张'),
      uploadPanel('上传新的底图', '这是另起一张图，会切换底图；只想添加参考，请用上面的“添加照片”。原方案和图片全部保留。')),
    pending ? null : el('div', { class: 'zs-editor-actions' },
      el('small', {}, comparing || changesOpen ? '对比中 · 退出后可生成' : active ? `底图：${conceptLabel(concept.id)} · 版本 ${active.version}` : `按${conceptLabel(concept.id)}生成`),
      active ? generationControl(generate, () => editIssue(active, change.querySelector('textarea').value)) : generate));
  view.addEventListener('keydown', event => {
    if (event.target.closest('input, textarea, select, summary') || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    const next = images[index + (event.key === 'ArrowLeft' ? -1 : 1)];
    if (!next) return;
    event.preventDefault();
    const fromStrip = event.target.closest('.zs-thumb');
    pick(next.id);
    if (fromStrip) view.querySelector('.zs-thumb[aria-current=true]')?.focus();
  });
  if (active && cloudImages()) {
    receiveReferences(active);
    const draft = currentEdit(active);
    if (!comparing && !changesOpen && draft.mode === 'local' && draft.selection) {
      const img = canvas.querySelector('[data-main-preview]'), overlay = selectionOverlay(draft.selection, active.width, active.height);
      canvas.append(overlay);
      const position = () => {
        if (!canvas.isConnected) return;
        const box = img.getBoundingClientRect(), parentBox = canvas.getBoundingClientRect();
        Object.assign(overlay.style, { width: box.width + 'px', height: box.height + 'px', left: box.left - parentBox.left + 'px', top: box.top - parentBox.top + 'px' });
      };
      img.addEventListener('load', position); queueMicrotask(position);
      selectionObserver = new ResizeObserver(position); selectionObserver.observe(img);
    }
  }
  if (changesOpen && parent) { const owner = task.id; queueMicrotask(() => showChanges(canvas, parent, active, owner)); }
  const siblings = active?.variant_group ? task.images.filter(i => i.variant_group === active.variant_group) : [];
  const comparison = siblings.length > 1 ? el('div', { class: 'zs-variant-comparison', 'aria-label': '同一底图的材质比较' },
    el('p', { class: 'zs-help zs-variant-note' }, '只比较石材，其他细节可能不同。'),
    [parent, ...siblings].filter(Boolean).map(image => el('button', { class: 'zs-variant', type: 'button', onclick: () => pick(image.id) },
      el('img', { src: imageURL(image.id), alt: '', loading: 'lazy' }),
      el('span', {}, image.id === parent?.id ? `修改前 · 版本 ${image.version}` : `${image.edits?.map(v => v.name).join(' / ') || '材质'} · 版本 ${image.version}`)))) : null;
  return [el('div', { class: 'zs-workspace' },
    el('div', { class: 'zs-stage' }, canvas, caption, comparison, images.length ? versionStrip(images, active, pick) : null), editor)];
}
function visionView(image) {
  const check = (task.visual_checks || []).filter(v => v.image_id === image.id).at(-1);
  if (!status.vision_ready && !check) return [];
  const standard = el('textarea', { name: 'vision_standard', maxlength: '1200', rows: '3', disabled: busy(), 'aria-label': '检查标准',
    placeholder: '可以不填。填了就只按这里检查，例如：底座是哑光黑色，莲瓣和上一版一致…' });
  return [el('details', { class: 'zs-check-details' }, el('summary', {}, `自动检查（版本 ${image.version}）`),
    el('p', { class: 'zs-help' }, '由程序看图比对外观和数量，结果只作参考，不代表有人看过，也不会改动存档里的检查情况。'),
    check ? el('div', { class: 'zs-check-results' },
      el('p', { class: 'zs-help' }, '检查于 ' + dateText(check.created)),
      el('p', { class: 'zs-help' }, '核对依据：' + (check.basis || '历史记录未注明，不能据此推定生成时的要求。')),
      ...check.checks.map(v => el('div', { class: 'zs-check-row', 'data-verdict': v.verdict },
        el('strong', {}, `${v.label}：${v.verdict}`), el('p', {}, v.observation))),
      el('p', { class: 'zs-help' }, userMessage(check.limitation))) : el('p', { class: 'zs-help' }, '这一版还没检查过。'),
    status.vision_ready ? standard : null,
    status.vision_ready ? paidButton(check ? '重新检查这一版' : '检查这一版', authorization => {
      imageSelection.set(task.id, image.id);
      return action('check', { image_id: image.id, ...authorization, standard: standard.value });
    }, 'check') : null)];
}
function reviewedVersion() {
  const versions = task.images.filter(image => image.concept_id === task.selected);
  return task.images.find(image => image.id === imageSelection.get(task.id)) ||
    versions.find(image => image.id === task.review.image_id) || versions.at(-1) ||
    (!task.selected && task.interior_input ? task.images.findLast(image => image.kind !== 'upload') : undefined);
}
function reviewValues(image) {
  const latest = task.images.filter(row => row.concept_id === task.selected).at(-1);
  const recorded = task.review.image_id || latest?.id;
  if (recorded === image?.id) return task.review;
  return (task.review_history || []).findLast(row => row.image_id === image?.id) ||
    { reviewer: task.review.reviewer, design: '待核验', culture: '待核验', process: '待核验' };
}
function reviewView() {
  const image = reviewedVersion();
  const versions = task.images.filter(row => row.concept_id === image?.concept_id);
  if (!image) return [empty('还没有可以存档的图片', '先生成一张图，再回来存档。', button('去生成图片', () => { supersedeRouteLoad(); stage = task.selected ? 3 : 2; render(); }, true, false))];
  const review = reviewValues(image);
  const form = el('form', { class: 'zs-card zs-review', 'data-review': '1', onsubmit: async e => {
    e.preventDefault(); try { if (await action('review', Object.fromEntries(new FormData(form)))) tell('检查情况已记下。'); } catch (err) { tell(err.message, true); }
  } }, el('input', { type: 'hidden', name: 'image_id', value: image.id }), field('reviewer', review.reviewer, null, '整理人'));
  form.querySelector('[name=reviewer]').required = true;
  form.querySelector('[name=reviewer]').placeholder = '填写你的名字';
  const checked = ['design', 'culture', 'process'].some(key => review[key] && review[key] !== '待核验');
  const checks = el('details', { class: 'zs-details zs-review-checks', open: checked },
    el('summary', {}, '检查情况', el('span', { class: 'zs-optional' }, '选填')),
    ...[['design', '造型与视觉'], ['culture', '文化与宗教细节'], ['process', '材料与工艺']].map(([key, text]) =>
      segmented(key, review[key] || '待核验', [['待核验', '待确认'], ['通过', '通过'], ['不通过', '不通过']], text, { class: 'zs-seg-field zs-verdict' })),
    el('p', { class: 'zs-help' }, '由看过这一版的人填写，不填就记为待确认。' + (status.vision_ready || task.visual_checks?.length ? '自动检查的结果不会填到这里。' : '')),
    el('button', { type: 'submit', class: 'zs-btn small', disabled: busy() }, '保存检查记录'));
    if (image.floorplan_snapshot || image.floorplan_input?.mode === 'floorplan_photographic') checks.insertBefore(el('section', { class: 'zs-floorplan-checks' },
    el('h3', {}, image.floorplan_snapshot ? '平面试例人工核对' : '室内实景人工核对'), floorplanBasis(image.floorplan_snapshot || null),
    interiorBasis(image.floorplan_input?.mode === 'floorplan_photographic' ? image.floorplan_input : null),
    ...[['walls', '墙体'], ['openings', '开口'], ['zones', '功能区'], ['objects', '物件数量与位置'], ['view', '视角']].map(([key, label]) =>
      segmented('floor_' + key, review.floorplan_checks?.[key] || '待核验',
        [['待核验', '待确认'], ['通过', '相符'], ['不通过', '不符']], label, { class: 'zs-seg-field zs-verdict' })),
    el('p', { class: 'zs-help' }, '对照这一版对应的原始平面图或结构底稿填写；相符不代表尺寸实测或施工合格，无法判断就保持待确认。')),
    checks.querySelector('button[type=submit]'));
  const save = button('存入资料库', async () => {
    if (!form.reportValidity()) return;
    if (!await action('review', Object.fromEntries(new FormData(form)))) return;
    let archived;
    try { archived = await action('archive', { image_id: image.id }); }
    catch (error) { tell('检查记录已保存，存档尚未完成。' + userMessage(error.message), true); return; }
    if (archived) {
      tell('已存入资料库，也新建了笔记。');
      try { await savePicture(image); } catch { tell('资料库和笔记已存好，但效果图文件夹未能导出。可以稍后再点保存。', true); }
    }
  }, true);
  form.append(...[checks, el('p', { class: 'zs-help' }, '检查记录按图片版本保留；单独保存不会切换选定方向。存入资料库时，会将这张图所属方案设为选定方向。存档不代表客户已经认可，也不能当作尺寸图或加工图。'),
    !task.review.image_id && ['design', 'culture', 'process'].some(key => task.review[key] && task.review[key] !== '待核验')
      ? el('p', { class: 'zs-help' }, '历史检查没有标记图片版本。请先核对这张图，再保存明确绑定的检查记录。') : null,
    el('div', { class: 'zs-form-actions' }, save)].filter(node => node != null));
  const feedback = el('form', { class: 'zs-feedback', onsubmit: async e => {
    e.preventDefault(); try { if (await action('feedback', Object.fromEntries(new FormData(feedback)))) tell('意见已保存。'); } catch (err) { tell(err.message, true); }
  } }, el('input', { type: 'hidden', name: 'image_id', value: image.id }),
    field('decision', '待客户确认', ['继续深化', '待客户确认', '不采用'], '结论'), field('comment', '', null, '原因和想改的地方'),
    el('p', { class: 'zs-help' }, `${conceptLabel(image.concept_id)} · 版本 ${image.version} 的意见。只有这个版本已有人工检查，且意见不再待客户确认，才会供同类设计参考。`),
    el('button', { type: 'submit', class: 'zs-btn', disabled: busy() }, '保存意见'));
  feedback.querySelector('[name=comment]').required = true;
  const pick = id => { supersedeRouteLoad(); imageSelection.set(task.id, id); render(); };
  return [el('div', { class: 'zs-save' },
    el('div', { class: 'zs-save-image' },
      el('figure', {}, el('img', { src: imageURL(image.id), alt: `要存档的图片：版本 ${image.version}`, width: image.width, height: image.height }),
        el('figcaption', {}, el('strong', {}, `选定版本 ${image.version}`), el('span', {}, versionNote(image, versions)), el('span', { class: 'zs-num' }, `${image.width} × ${image.height}`))),
      saveControls(image), button('在这张上继续修改', () => { routePush = true; stage = 3; return useImage(image); }, false, uploading || planning),
      versions.length > 1 ? el('div', {}, el('p', { class: 'zs-help' }, '换一版存档：'), versionStrip(versions, image, pick, '选择要存档的版本')) : null),
    el('div', { class: 'zs-save-side' },
      heading('存入资料库', '保存这个方案的各版图片，并生成说明笔记。'),
      form,
      el('details', { class: 'zs-details' }, el('summary', {}, '新增意见'), feedback),
      task.feedback.length ? el('div', { class: 'zs-feedback-list' }, el('h4', {}, '已保存的意见'),
        task.feedback.slice().reverse().map(f => {
          const version = task.images.find(row => row.id === f.image_id);
          return el('p', {}, el('small', {}, version ? `${conceptLabel(version.concept_id)} · 版本 ${version.version} · ` : '历史意见，未标记图片版本 · '),
            el('strong', {}, f.decision), '：' + f.comment);
        })) : null,
      el('div', { class: 'zs-archives' }, task.archives.length ? el('h4', {}, '已存档') : null,
        task.archives.map(a => el('a', { href: noteURL(a.note), class: 'zs-link' }, icon('note'), '打开存档笔记' + (dateText(a.created) ? '（' + dateText(a.created) + '）' : '')))),
      button('查看资料库', () => { routePush = true; libraryTab = 'archive'; return openLibrary(); }, false, false, { class: 'zs-btn quiet' })))];
}

function matchesProject(value, query) {
  const brief = value.brief || value;
  const text = [brief.title, brief.category, brief.goal, brief.materials, brief.setting].filter(Boolean).join(' ').normalize('NFKC').toLocaleLowerCase();
  return query.normalize('NFKC').trim().toLocaleLowerCase().split(/\s+/).every(word => text.includes(word));
}
function listStatus(row) {
  if (row.job?.status === 'running') return '正在处理…';
  if (row.cloud_pending_count || ['failed', 'uncertain', 'interrupted'].includes(row.job?.status)) return '需要处理';
  if (row.job?.status === 'registration-pending' || row.job?.result_recovery?.length) return '待恢复登记';
  if (row.job?.local_recovery?.length) return '待继续合成';
  if ((row.interior_ready || row.interior_input) && !(row.image_count ?? row.images?.length ?? 0)) return '平面图已保存，可生成';
  if ((row.interior_ready || row.interior_input) && ['待比较方向', '待选定方案', '待挑方案'].includes(row.status)) return '已生成，可继续调整';
  return { '待构思': '还没出方案', '待比较方向': '待挑方案', '待选定方案': '待挑方案', '待深化与评审': '修改中', '待评审': '修改中',
    '已上传图片，待设计': '已上传照片', '已归档概念版本': '已存档', '任务已更新，待重新构思': '要求已改' }[row.status] || row.status || '';
}
function readSeen(storage = localStorage) {
  let value;
  try { value = JSON.parse(storage.getItem('zyb.studio.seen') || '{}'); } catch { value = {}; }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).filter(([, count]) => Number.isInteger(count) && count >= 0));
}
function writeSeen(seen, storage = localStorage) { try { storage.setItem('zyb.studio.seen', JSON.stringify(seen)); } catch {} }
function markUnseen(rows, seen, storage = localStorage) {
  let changed = false;
  for (const row of rows) if (!Number.isInteger(seen[row.id])) { seen[row.id] = row.image_count || 0; changed = true; }
  if (changed) writeSeen(seen, storage);
  return changed;
}
function freshImages(row, seen) { return Number.isInteger(seen[row.id]) ? Math.max(0, (row.image_count || 0) - seen[row.id]) : 0; }
function renderProjectList() {
  const list = panel.querySelector('.zs-task-list'); if (!list) return;
  const focused = list.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
  const seen = readSeen();
  markUnseen(tasks, seen);
  const matching = tasks.filter(t => matchesProject(t, projectQuery));
  const shown = matching.filter(t => !t.hidden), hidden = matching.filter(t => t.hidden);
  list.replaceChildren(...shown.map(t => {
    const fresh = freshImages(t, seen);
    return el('button', { type: 'button', class: 'zs-task', disabled: planning || uploading, 'data-focus': 'task-' + t.id,
      onclick: () => { hideNotice(); chooseTask(t.id, { push: true }).catch(e => tell(e.message, true)); } },
      el('span', { class: 'zs-task-cover' }, t.cover ? el('img', { src: imageURL(t.cover.id, t.id), alt: '', loading: 'lazy', width: t.cover.width, height: t.cover.height })
        : el('span', { class: 'zs-cover-empty' }, icon('photo')), fresh ? el('span', { class: 'zs-new-badge' }, `${fresh} 张新图`) : null),
      el('strong', {}, t.brief.title),
      el('span', { class: 'zs-task-meta' }, el('span', {}, listStatus(t)), t.updated ? el('time', { datetime: t.updated }, dateText(t.updated)) : null));
  }));
  if (!shown.length) list.append(el('p', { class: 'zs-help zs-list-empty' }, projectQuery ? `没有找到和“${projectQuery.trim()}”相关的设计，试试名称、材质或用途。` : '做过的设计会出现在这里。'));
  panel.querySelector('.zs-hidden-list')?.replaceChildren(...(hidden.length ? [el('details', { class: 'zs-details zs-hidden' },
    el('summary', {}, `已隐藏的设计（${hidden.length}）`),
    el('ul', {}, hidden.map(row => el('li', {}, el('span', {}, row.brief.title),
      button('恢复', async () => {
        await api('tasks/' + row.id + '/unhide', { revision: row.revision });
        await loadTasks(); renderProjectList(); tell('已恢复到「我的设计」。');
      }, false, planning || uploading, { class: 'zs-btn quiet small', 'data-focus': 'unhide-' + row.id })))))] : []));
  if (focused) list.querySelector(`[data-focus="${focused}"]`)?.focus({ preventScroll: true });
}
async function openLibrary() {
  closeInteriorPhotos();
  hideNotice();
  const push = routePush;
  const epoch = ++navigation;
  routeLoadEpoch = epoch; routePush = false;
  clearTimeout(pollTimer);
  let result;
  try { result = await api('library'); }
  catch (error) {
    if (epoch !== navigation || !panel?.open) return false;
    routeLoadEpoch = null; routePush = false; syncRoute(); schedule();
    throw Error('资料库暂未打开，仍在原页面。' + error.message, { cause: error });
  }
  if (epoch !== navigation || !panel?.open) return;
  libraryItems = result.items; task = null; stage = 5; comparing = false; resetReferences();
  routeLoadEpoch = null; routePush = push; render();
}
function moreMenu(...actions) {
  const summary = el('summary', {}, '更多');
  const menu = el('div', { class: 'zs-more-menu', 'aria-label': '更多操作' }, actions);
  const details = el('details', { class: 'zs-more' }, summary, menu);
  const popover = typeof menu.showPopover === 'function';
  if (popover) menu.setAttribute('popover', 'manual');
  let listeners;
  const close = () => {
    details.open = false;
    listeners?.abort(); listeners = null;
    if (popover && menu.matches(':popover-open')) menu.hidePopover();
    if (closeMoreMenu === close) closeMoreMenu = null;
  };
  const position = () => {
    if (!details.open || !details.isConnected || !panel.open) { close(); return; }
    const anchor = summary.getBoundingClientRect(), clip = (details.closest('.zs-main') || panel).getBoundingClientRect();
    const viewport = window.visualViewport;
    const left = (viewport?.offsetLeft || 0) + 8;
    const top = Math.max((viewport?.offsetTop || 0) + 8, clip.top + 8);
    const right = left + (viewport?.width || window.innerWidth) - 16;
    const bottom = Math.min((viewport?.offsetTop || 0) + (viewport?.height || window.innerHeight) - 8, clip.bottom - 8);
    if (anchor.bottom <= Math.max(clip.top, top) || anchor.top >= Math.min(clip.bottom, bottom)) { close(); return; }
    menu.style.maxWidth = `${Math.max(0, right - left)}px`;
    menu.style.maxHeight = `${Math.max(0, bottom - top)}px`;
    const natural = menu.getBoundingClientRect();
    const below = bottom - anchor.bottom - 6, above = anchor.top - top - 6;
    const upwards = below < natural.height && above > below;
    menu.dataset.placement = upwards ? 'above' : 'below';
    menu.style.maxHeight = `${Math.max(0, upwards ? above : below)}px`;
    const size = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(left, Math.min(anchor.right - size.width, right - size.width))}px`;
    menu.style.top = `${Math.max(top, Math.min(upwards ? anchor.top - size.height - 6 : anchor.bottom + 6, bottom - size.height))}px`;
  };
  details.addEventListener('toggle', () => {
    if (!details.open) { close(); return; }
    if (closeMoreMenu !== close) closeMoreMenu?.();
    closeMoreMenu = close;
    if (popover && !menu.matches(':popover-open')) menu.showPopover();
    listeners?.abort(); listeners = new AbortController();
    const options = { capture: true, passive: true, signal: listeners.signal };
    panel.addEventListener('scroll', position, options);
    window.addEventListener('resize', position, options);
    window.visualViewport?.addEventListener('resize', position, options);
    window.visualViewport?.addEventListener('scroll', position, options);
    position();
  });
  if (popover) menu.addEventListener('toggle', event => { if (event.newState === 'closed') close(); });
  menu.addEventListener('click', event => {
    if (!event.target.closest('button, a')) return;
    close(); summary.focus({ preventScroll: true });
  });
  return details;
}
function archiveShots(item, archive) {
  const url = image => base + 'archive-image/' + item.id + '/' + archive.index + '/' + image;
  if (archive.image_id) return [{ id: archive.image_id, label: archive.image ? `版本 ${archive.image.version}` : '存档图片',
    extension: archive.image?.extension || 'png', url: archive.image && archive.image_state !== 'missing' ? url(archive.image_id) : null }];
  return (archive.legacy_images || []).map((image, i) => ({ id: image.id, label: image.label || `第 ${i + 1} 张`,
    extension: image.extension || 'png', url: image.state === 'ok' ? url(image.id) : null }));
}
function legacyCaption(shots) {
  const missing = shots.filter(shot => !shot.url).length;
  return `存档时没有标记选定版本 · 当时存下 ${shots.length} 张` + (missing ? `，其中 ${missing} 张找不到了` : '');
}
function archiveFileName(title, shot) {
  const name = title.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '').trim().slice(0, 50) || '设计图';
  return `${name}-${shot.label.replace(/\s+/g, '')}.${shot.extension}`;
}
function libraryCard(item) {
  const records = [...item.archives].reverse();
  const select = records.length > 1 ? el('select', { 'aria-label': '保存记录' }, records.map((a, i) =>
    el('option', { value: String(a.index) }, (dateText(a.created) || `第 ${a.index + 1} 次保存`) + (i === 0 ? '（最近）' : '')))) : null;
  const preview = el('div', { class: 'zs-library-preview' }), meta = el('p', { class: 'zs-meta' });
  const caption = el('p', { class: 'zs-help zs-archive-caption', hidden: true }), downloads = el('span', { class: 'zs-downloads' });
  const dimensions = el('div', { class: 'zs-archive-dimensions', 'aria-label': '存档尺寸记录' });
  const note = el('a', { class: 'zs-btn quiet small' }, icon('note'), '打开笔记');
  const current = () => item.archives[Number(select?.value ?? records[0].index)];
  const lost = (...lines) => el('div', { class: 'zs-unrendered zs-legacy' }, icon('note'), lines.map(line => el('span', {}, line)));
  const link = (shot, ...text) => el('a', { class: 'zs-btn quiet small', href: shot.url, download: archiveFileName(item.title, shot) }, ...text);
  const show = () => {
    const archive = current(), shots = archiveShots(item, archive), ready = shots.filter(shot => shot.url);
    note.href = noteURL(archive.note);
    meta.replaceChildren(...[el('span', {}, item.category), el('span', {}, '存于 ' + dateText(archive.created)),
      archive.image ? el('span', {}, `版本 ${archive.image.version}`) : null].filter(Boolean));
    caption.textContent = archive.image_id || !shots.length ? '' : legacyCaption(shots); caption.hidden = !caption.textContent;
    if (archive.image_id) preview.replaceChildren(ready.length
      ? el('img', { src: ready[0].url, alt: `${item.title}，${ready[0].label}`, loading: 'lazy', width: archive.image.width, height: archive.image.height })
      : lost('这张存档图片找不到了。', '请联系管理员核对。'));
    else if (shots.length) preview.replaceChildren(el('div', { class: 'zs-archive-shots', 'data-count': String(Math.min(shots.length, 4)) },
      shots.slice(0, 4).map((shot, i) => el('figure', { class: 'zs-shot' + (shot.url ? '' : ' missing') },
        shot.url ? el('img', { src: shot.url, alt: `${item.title}，${shot.label}`, loading: 'lazy' }) : el('span', {}, '找不到了'),
        el('figcaption', {}, shot.label), i === 3 && shots.length > 4 ? el('span', { class: 'zs-shot-more' }, `+${shots.length - 4}`) : null))));
    else preview.replaceChildren(lost('存档时没有标记选定版本。', '笔记里没找到当时的图片，可以打开笔记看看。'));
    if (ready.length > 1) {
      const menu = moreMenu(...ready.map(shot => link(shot, shot.label)));
      menu.querySelector('summary').replaceChildren(icon('download'), '下载…');
      menu.querySelector('.zs-more-menu').setAttribute('aria-label', '选择要下载的图片');
      downloads.replaceChildren(menu);
    } else downloads.replaceChildren(...ready.map(shot => link(shot, icon('download'), '下载')));
    if (archive.image_id && ready.length) downloads.prepend(button('保存到效果图文件夹', async () => {
      const result = await api('library/save', { task_id: item.id, index: archive.index, image_id: archive.image_id });
      tell('存档图片已保存到这台 Mac。'); panel.querySelector('.zs-notice > span').append(el('code', { class: 'zs-save-path' }, result.path));
    }, false, false, { class: 'zs-btn quiet small' }));
    dimensions.replaceChildren();
    if (archive.evidence_bundle) {
      dimensions.append(archive.evidence_state === 'ok'
        ? el('a', { class: 'zs-btn quiet small', href: base + 'archive-evidence/' + item.id + '/' + archive.index,
            download: `design-evidence-${item.id.slice(0, 12)}-${archive.evidence_bundle.sha256.slice(0, 12)}.zip` }, '下载版本依据包 ZIP')
        : el('p', { class: 'zs-help' }, '版本依据包缺失或校验异常，请联系管理员核对。'));
      const basis = archive.evidence_bundle;
      if (basis.missing_input_count || basis.unknown_basis_count || basis.unverified_input_count || basis.unverified_version_count) dimensions.append(el('p', { class: 'zs-help' },
        `依据缺口：${basis.missing_input_count || 0} 条资料记录不可定位，${basis.unknown_basis_count || 0} 个版本未记录生成时要求，${basis.unverified_input_count || 0} 条图片参考未保存生成时内容校验值，${basis.unverified_version_count || 0} 个历史版本未保存登记时内容校验值；包内清单保留这些缺口。`));
    } else dimensions.append(el('p', { class: 'zs-help' }, '这份历史存档未打包版本依据；当前参考资料不代表当时的记录。'));
    for (const record of archive.dimension_files || []) {
      const url = base + 'archive-dimension/' + item.id + '/' + archive.index + '/' + record.image_id + '/' + record.document_sha256;
      const label = `版本 ${record.version} · ${record.item_count} 条尺寸标注`;
      dimensions.append(el('div', { class: 'zs-row' }, el('span', { class: 'zs-help' }, label),
        ...[['png', '下载尺寸图'], ['json', '下载尺寸记录 JSON']].map(([format, name]) => record[format + '_state'] === 'ok'
          ? el('a', { class: 'zs-btn quiet small', href: url + '?format=' + format,
              download: `dimensions-v${record.version}-${record.image_id.slice(0, 12)}.${format}` }, name)
          : el('span', { class: 'zs-help' }, name + '：文件缺失或校验异常，请联系管理员核对。'))));
    }
    if (!archive.dimension_files?.length) dimensions.append(el('p', { class: 'zs-help' }, archive.dimension_schema === 1
      ? '本次存档没有已保存的尺寸标注。' : '这份历史存档未打包尺寸记录；当前图片上的标注不代表当时的记录。'));
  };
  select?.addEventListener('change', show); show();
  const finder = (target, done) => async () => {
    await api('library/open', { task_id: item.id, index: current().index, target });
    tell(done);
  };
  return el('article', { class: 'zs-library-card' }, preview,
    el('div', { class: 'zs-library-body' }, el('h3', {}, item.title), meta, caption, select,
      el('div', { class: 'zs-library-actions' },
        button('打开设计', async () => {
          const imageId = current().image_id;
          if (!await chooseTask(item.id, { push: true })) return;
          const image = task.images.find(row => row.id === imageId);
          if (image) { stage = 3; return useImage(image); }
          galleryOpen = true; go(3);
        }, false, false, { class: 'zs-btn small' }), downloads, note,
      moreMenu(
          button('在访达查看图片', finder('images', '已在工作台所在的 Mac 上打开这次存档的图片文件夹。'), false, false, { class: 'zs-btn quiet small' }),
          button('在访达定位笔记', finder('note', '已在工作台所在的 Mac 上用访达找到这篇笔记。'), false, false, { class: 'zs-btn quiet small' }))), dimensions));
}
function libraryView() {
  const tabs = el('nav', { class: 'zs-steps', 'aria-label': '资料库分类' }, [['archive', '设计存档'], ['refs', '参考素材']].map(([key, name]) =>
    el('button', { type: 'button', class: libraryTab === key ? 'active' : '', 'aria-current': libraryTab === key ? 'page' : 'false', 'data-focus': 'library-' + key,
      onclick: () => { if (libraryTab === key) return; supersedeRouteLoad(); routePush = true; hideNotice(); libraryTab = key; render(); view.scrollTop = 0; } }, name)));
  if (libraryTab === 'refs') return [heading('资料库'), el('div', { class: 'zs-section-head' }, tabs, referenceSearch()),
    el('p', { class: 'zs-help zs-library-note' }, '这里是收集来的参考图和客户提供的资料，只能查看。新建设计时可以从中挑几份作参考。'),
    el('div', { class: 'zs-reference-grid' })];
  const grid = el('div', { class: 'zs-library-grid' });
  const search = el('input', { type: 'search', value: libraryQuery, placeholder: '搜名称、材质或用途…', 'aria-label': '搜索资料库', autocomplete: 'off' });
  const update = () => {
    closeMoreMenu?.();
    grid.replaceChildren(...libraryItems.filter(v => matchesProject(v, search.value)).map(libraryCard));
    if (!grid.children.length) grid.append(empty(search.value.trim() ? '没有找到匹配的存档' : '还没有存档的设计',
      search.value.trim() ? '试试名称、材质或用途。' : '在设计的图片页点「存入资料库…」，存好的设计会出现在这里。'));
  };
  search.addEventListener('input', () => { libraryQuery = search.value; update(); }); update();
  return [heading('资料库'), el('div', { class: 'zs-section-head' }, tabs, el('label', { class: 'zs-search-field' }, icon('search'), search)), grid];
}
function designTabs() {
  const tabs = [[0, '要求']];
  if (task.interior_input || task.concepts.some(c => !c.source_image_id) || !task.selected) tabs.push([task.interior_input || task.concepts.length ? 2 : 1, task.interior_input ? '空间' : '方案']);
  if (task.selected || task.images.length || task.edit_assets?.length || compositionTask === task.id) tabs.push([3, '图片']);
  if (task.images.length) tabs.push([4, '存档']);
  return tabs;
}
function designBar() {
  const tabs = designTabs();
  const current = stage === 1 ? 2 : stage;
  return el('div', { class: 'zs-design-bar' },
    el('button', { class: 'zs-back', type: 'button', 'aria-label': '返回我的设计', 'data-focus': 'back', disabled: planning || uploading,
      onclick: () => { routePush = true; goHome(); } }, icon('back')),
    renaming != null ? renameForm() : el('h1', { class: 'zs-design-title', title: task.brief.title }, task.brief.title),
    renaming != null ? null : moreMenu(
      button('改名', () => {
        renaming = task.brief.title; render();
        const input = panel.querySelector('.zs-rename input'); input?.focus(); input?.select();
      }, false, planning || uploading, { class: 'zs-btn quiet small' }),
      button('隐藏这份设计', hideDesign, false, busy(), { class: 'zs-btn quiet small' })),
    el('nav', { class: 'zs-steps', 'aria-label': '这份设计' }, tabs.map(([target, name]) => {
      const active = (target === 1 ? 2 : target) === current;
      return el('button', { type: 'button', class: active ? 'active' : '', 'aria-current': active ? 'step' : 'false', 'data-focus': 'tab-' + target,
        disabled: planning || uploading, onclick: () => go(target) }, name);
    })));
}
function renameForm() {
  const input = el('input', { type: 'text', name: 'rename_title', maxlength: '150', 'aria-label': '设计名称', autocomplete: 'off', 'data-draft': 'off',
    oninput: () => { renaming = input.value; } });
  input.value = renaming ?? task.brief.title;
  return el('form', { class: 'zs-rename', onsubmit: async event => {
    event.preventDefault();
    if (!input.value.trim()) return tell('名称不能为空。', true);
    try { if (await action('rename', { title: input.value.trim() })) { renaming = null; render(); tell('已改名。'); } }
    catch (error) { tell(error.message, true); }
  } }, input, el('button', { class: 'zs-btn primary small', type: 'submit' }, '保存'),
    button('取消', () => { renaming = null; render(); }, false, false, { class: 'zs-btn quiet small' }));
}
async function hideDesign() {
  if (busy()) return;
  if (!window.confirm('隐藏后，这份设计不会出现在「我的设计」和「资料库」里。图片和笔记都不会删除，随时可以在「我的设计」底部恢复。确定隐藏吗？')) return;
  if (await action('hide')) { routePush = true; goHome(); tell('已隐藏。可以在「我的设计」底部的「已隐藏的设计」里恢复。'); }
}
function stopKey(owner, job) {
  return owner + ':' + (job.operation_id || 'legacy');
}
async function cancelJob(job, message) {
  const id = task.id, epoch = navigation, operation = job.operation_id, key = stopKey(id, job);
  if (pendingStops.has(key)) return false;
  pendingStops.add(key); render();
  try {
    const sent = await action('cancel', operation ? { operation_id: operation } : {});
    if (sent && currentTask(id, epoch) && task.job?.status === 'running' && task.job.cancel
        && (!operation || task.job.operation_id === operation)) tell(message);
    return sent;
  } finally {
    pendingStops.delete(key);
    if (currentTask(id, epoch) && panel?.open) { render(); schedule(); }
  }
}
function jobBanner() {
  const job = task.job, stop = stopControl(job);
  return el('div', { class: 'zs-job' + (job.result_recovery?.length && job.status !== 'running' ? ' zs-job-recovery' : ''), 'data-state': job.status, role: 'status' },
    job.status === 'running' ? el('span', { class: 'zs-spinner', 'aria-hidden': 'true' }) : null, el('span', {}, jobMessage(job)),
    stop ? button(stop[0], () => cancelJob(job, stop[1]), false, pendingStops.has(stopKey(task.id, job)), { class: 'zs-btn quiet small' }) : null,
    job.result_recovery?.length && job.status !== 'running' ? el('div', { class: 'zs-row' },
      el('p', { class: 'zs-help' }, '只登记本机已有图片，不调用生图服务；恢复后仍需人工核对。'),
      ...job.result_recovery.map((id, index) => el('a', { class: 'zs-btn quiet small', target: '_blank', rel: 'noopener',
        href: base + `registration-result/${task.id}/${id}` }, `查看待登记结果 ${index + 1}`)),
      button('恢复已有结果', recoverResults, false, busy(), { class: 'zs-btn quiet small' })) : null,
    job.local_recovery?.length && job.status !== 'running' ? el('div', { class: 'zs-row' },
      button('继续处理已生成的图片', () => action('finish-local'), false, busy(), { class: 'zs-btn quiet small' }),
      ...job.local_recovery.map((id, index) => el('a', { class: 'zs-btn quiet small', target: '_blank', rel: 'noopener', href: base + `local-result/${task.id}/${id}` }, `查看未合成结果 ${index + 1}`)),
      button('结束本次合成', () => {
        if (window.confirm('结束本次合成后，可以重新修改。已生成的原始图片仍保存在本机，但不会作为正式版本。确定结束吗？')) return action('discard-local', { confirmed: true });
      }, false, busy(), { class: 'zs-btn quiet small' })) : null);
}

function cloudHistoryNotice() {
  const history = task?.cloud_job_history || [];
  const valid = row => row && typeof row === 'object' && !Array.isArray(row);
  const damaged = !Array.isArray(history) || history.some(row => !valid(row));
  const rows = Array.isArray(history) ? history.filter(valid) : [];
  if (damaged) rows.push({ unreadable: true });
  let current = null;
  if (task?.job?.status === 'uncertain') {
    const index = rows.findIndex(row => task.job.operation_id && row.operation_id === task.job.operation_id);
    current = { ...(index >= 0 ? rows[index] : {}), ...task.job, continued_at: null };
    if (index >= 0) rows[index] = current;
    else rows.push(current);
  }
  if (!rows?.length) return null;
  const pending = rows.filter(row => !row.unreadable && !row.continued_at);
  const label = row => row.cloud_operation === 'plan' ? '方案构思'
    : row.cloud_operation === 'vision' || row.action === 'check' ? '视觉核对' : '图片生成';
  const details = el('details', { class: 'zs-details' }, el('summary', {}, `查看云端未确认记录（${rows.length}）`),
    rows.slice().reverse().map(row => row.unreadable
      ? el('p', { class: 'zs-help' }, '部分生成记录未能读取，已有图片仍可查看、保存和下载。请联系管理员核对后再生成。')
      : el('div', {},
      el('p', { class: 'zs-help' }, `${row === current ? '本次 · ' : ''}${label(row)} · ${dateText(row.started || row.retained_at)} · 结果未确认`),
      el('p', { class: 'zs-help' }, row.operation_id ? `操作编号：${row.operation_id}`
        : '此记录未保存操作编号。请提供设计名称与请求时间给管理员核对。'),
      el('p', { class: 'zs-help' }, row.continued_at
        ? '已继续其他操作，这次的完整结果仍未收到。'
        : '请联系管理员核对服务记录；查看、下载和保存已有图片不会确认这次请求。'))));
  return el('div', { class: 'zs-job', 'data-cloud-history': '1', role: 'status' }, el('div', {},
    pending.length ? el('p', { class: 'zs-help' }, `${pending.length} 次云端请求仍未确认。高清处理不会改变这些状态。`) : null,
    details));
}

function startNew() {
  if (task || stage !== 0) { routePush = true; goHome(); }
  const goal = view?.querySelector('[name=goal]');
  goal?.scrollIntoView({ block: 'center' }); goal?.focus({ preventScroll: true });
}
function focusKey(node) {
  if (!node || node === document.body || !panel?.contains(node)) return null;
  const text = node.selectionStart != null ? [node.selectionStart, node.selectionEnd] : null;
  if (node.dataset.focus) return { selector: `[data-focus="${node.dataset.focus}"]` };
  if (node.name) return { selector: node.type === 'radio' ? `[name="${node.name}"][value="${CSS.escape(node.value)}"]` : `[name="${node.name}"]`, text };
  const label = node.getAttribute('aria-label') || (node.tagName === 'BUTTON' || node.tagName === 'SUMMARY' ? node.textContent.trim() : '');
  return label ? { tag: node.tagName, label } : null;
}
function restoreFocus(key, samePage) {
  if (!key || (document.activeElement && document.activeElement !== document.body && panel.contains(document.activeElement))) return;
  const node = key.selector ? panel.querySelector(key.selector)
    : [...panel.querySelectorAll(key.tag)].find(n => (n.getAttribute('aria-label') || n.textContent.trim()) === key.label);
  if (node && !node.disabled && (samePage || key.selector?.startsWith('[data-focus'))) {
    node.focus({ preventScroll: true });
    if (key.text && node.setSelectionRange) try { node.setSelectionRange(...key.text); } catch {}
  } else if (!samePage) view.focus({ preventScroll: true });
}
function render() {
  if (!panel) return;
  stage = designStage(stage);
  selectionObserver?.disconnect(); selectionObserver = null;
  const menuOpen = Boolean(panel.querySelector('.zs-design-bar .zs-more[open]'));
  closeMoreMenu?.();
  const focus = focusKey(document.activeElement);
  rememberInputs();
  const same = renderedStage === stage && renderedTask === task?.id && view;
  const cloudHistoryOpen = same && panel.querySelector('[data-cloud-history] details')?.open;
  const scroll = same ? view.scrollTop : 0;
  const disclosureKey = node => node.dataset.disclosure || node.querySelector(':scope > summary')?.firstChild?.textContent.trim();
  const opened = same ? new Set([...view.querySelectorAll('details[open]')].map(disclosureKey)) : new Set();
  renderedStage = stage; renderedTask = task?.id;
  if (!same) renaming = null;
  draftKey = `${task?.id || 'new'}:${stage}` + (task && stage === 4 ? ':' + (reviewedVersion()?.id || '') : '');
  if (!same && (stage === 1 || !task && stage === 0)) {
    const saved = drafts.get((task?.id || 'new') + ':refs')?.[0]?.[1];
    if (Array.isArray(saved)) picked = new Map(saved);
  }
  if (task && task.job?.status !== 'running') {
    const seen = seenImages.get(task.id), fresh = task.images.slice(seen ?? task.images.length).filter(image => image.kind !== 'upload');
    if (seen != null && fresh.length) {
      const references = fresh.every(image => image.kind === 'design');
      const interiorPhotos = references && fresh.every(image => image.floorplan_input?.mode === 'floorplan_photographic');
      tell(interiorPhotos ? `${fresh.length} 张室内效果图已生成。` : references ? `${fresh.length} 个参考方案已生成。` : fresh.length > 1 ? `${fresh.length} 张效果图已生成。底图没有切换。` : `版本 ${fresh[0].version} 已生成。底图没有切换。`);
      const image = fresh.at(-1), owner = task.id;
      panel.querySelector('.zs-notice > span').append(button(interiorPhotos ? '查看室内视角' : references ? '查看方案' : '查看新图', () => {
        if (task?.id === owner) { if (references) return go(2); stage = 3; return useImage(image); }
      }, false, false, { class: 'zs-btn quiet small' }));
    }
    seenImages.set(task.id, task.images.length);
    const seenCounts = readSeen();
    if (seenCounts[task.id] !== task.images.length) { seenCounts[task.id] = task.images.length; writeSeen(seenCounts); }
  }
  const home = !task && stage !== 5;
  panel.querySelector('.zs-nav').replaceChildren(
    el('button', { type: 'button', 'aria-current': home || task ? 'page' : 'false', 'data-focus': 'nav-home', disabled: planning || uploading,
      onclick: () => { routePush = true; goHome(); } }, '我的设计'),
    el('button', { type: 'button', 'aria-current': stage === 5 && !task ? 'page' : 'false', 'data-focus': 'nav-library', disabled: planning || uploading,
      onclick: () => { routePush = true; openLibrary().catch(error => tell(error.message, true)); } }, '资料库'));
  const content = panel.querySelector('.zs-content');
  const bar = same && renaming != null ? content.querySelector('.zs-design-bar') : null;
  if (bar?.querySelector('.zs-rename') && task && status) {
    const fresh = designBar();
    for (const part of ['.zs-back', '.zs-steps']) bar.querySelector(part)?.replaceWith(fresh.querySelector(part));
    for (const node of [...content.children]) if (node !== bar) node.remove();
  } else {
    content.replaceChildren();
    if (task && status) content.append(designBar());
    if (same && menuOpen) content.querySelector('.zs-design-bar .zs-more')?.setAttribute('open', '');
  }
  if (task?.job && task.job.status !== 'done') content.append(jobBanner());
  const cloudHistory = cloudHistoryNotice();
  if (cloudHistory) {
    if (cloudHistoryOpen) cloudHistory.querySelector('details').open = true;
    content.append(cloudHistory);
  }
  content.append(formDraftNotice());
  if (stylesError && status) content.append(el('p', { class: 'zs-help', role: 'status' }, stylesError));
  for (const [owner] of pendingSubmissions) content.append(el('div', { class: 'zs-job', role: 'status', 'data-submission-owner': owner },
    el('span', {}, '提交结果尚未确认，正在核对服务器记录。确认前不会再次生成。'),
    button(pendingChecks.has(owner) ? '正在核对…' : '核对并结束等待', () => endSubmissionWait(owner), false,
      pendingChecks.has(owner), { class: 'zs-btn quiet small' })));
  view = el('main', { class: 'zs-main', tabindex: '-1', 'data-stage': stage, 'data-view': task ? 'design' : stage === 5 ? 'library' : 'home' }); content.append(view);
  if (!status) {
    view.append(empty('暂时连不上工作台', '已经保存的设计不受影响。请确认工作台所在的 Mac 已开机并登录，再重新连接；仍然不行请联系管理员。', button('重新连接', openStudio, true, false)));
    return;
  }
  activeImageId = null;
  const renderers = [taskOverview, referenceView, conceptView, refineView, reviewView, libraryView];
  view.append(...renderers[stage]().filter(node => node != null));
  for (const node of view.querySelectorAll('details')) if (opened.has(disclosureKey(node))) node.open = true;
  view.scrollTop = scroll;
  if (stage === 3) {
    const previous = drafts.get(draftKey + `:${task.selected || 'none'}:${activeImageId || 'new'}`);
    draftKey += compositionTask === task.id ? ':compose' : activeImageId || !task.images.length ? ':edit:' + (activeImageId || 'new') : ':gallery';
    if (!drafts.has(draftKey) && previous) drafts.set(draftKey, previous);
  }
  if (stage === 1 || (stage === 5 && libraryTab === 'refs')) { renderRefs(); if (!refsSearched) void loadReferences(); }
  if (home) renderProjectList();
  restoreInputs();
  refreshValidationNotice();
  restoreFocus(focus, same);
  syncRoute();
  saveDrafts();
  schedule();
}
function helpText() {
  return [el('h2', {}, '怎么用'),
    el('p', {}, '只有文字要求：写下想做什么，点「出三个方案和效果图」。三个方案出来后会接着各出一张图，关掉页面也会继续。'),
    el('p', {}, '上传照片后先选用途：「在这张图上修改」会打开修改页，确认范围后再生成；「参考图片做新方案」可用一张或多张照片，分别指定材质、造型或风格，再选生成 1、2 或 3 个方案。原图和已有版本都保留。'),
    el('p', {}, '想让方案参考收集的图片或客户资料，新建时点「先挑参考素材」。「资料库」的「参考素材」一栏可以随时翻看。'),
    el('p', {}, '选好图片后点「存入资料库…」，填写整理人即可保存，之后在「资料库」的「设计存档」里找到。'),
    el('h2', {}, '选用风格和准备室内实景'),
    el('p', {}, '新建页的「浏览并选用」提供六个风格方向，实景、馆藏和原创概念分别标明来源。选用后先核对图片用途，只有确认生成时才发送素材。'),
    el('p', {}, '「平面图生成室内实景」上传平面图、选择风格后，点击生成即可开始；补充照片和空间要求都是可选的。也可以先调整要求。完成后对照平面图查看墙体、门窗和家具布置，再继续修改。'),
    el('p', {}, '室内设计中替换精选风格会同步更新主风格和所选参考；追加只增加参考。原始平面图和历史图片不变，手写说明需重新核对。'),
    el('p', {}, '「高级结构工具」中的「打开矩形结构试例」用于单个矩形空间研究，生成复核图、灰模与深度底稿；它不代替上传原始平面图的室内实景流程。'),
    el('h2', {}, '标注已知尺寸'),
    el('p', {}, '「标注已知尺寸」是独立辅助工具。填写部位、数值、单位、来源和依据，再点选或拖动两个端点。可以从已记录的图纸尺寸或设计目标带入，不会按效果图估算。每版单独保存；导出的尺寸图和记录文件不会改动原图。'),
    el('h2', {}, '继续修改已有方案'),
    el('p', {}, '在「全部图片」里选任意版本，点「在这张上改」。写下修改要求；需要参考照片时点「添加照片」，可一次传多张，也可以直接粘贴。添加参考不会换掉底图，新图另存一版。'),
    el('p', {}, '只想改某处、保留其余画面：选「只改选区」，再点「选择修改范围」。整张重新设计则用「整体调整」，其他细节也可能变化。'),
    el('h2', {}, '取照片中的一部分'),
    el('p', {}, '在参考图下点「选取局部」，选出想参考的内容。可以点选物体，也可框选、圈选或用画笔涂；多选了用「减选」，选错了用「撤销」。确认后，回到修改说明里写清楚怎么用。'),
    el('p', {}, '要精确指定放置位置，展开该参考图的「更多设置」。放置位置表示物件放哪儿；修改范围决定原图哪些地方可以改变，两者不同。'),
    el('h2', {}, '把多张图组合起来'),
    el('p', {}, '点「用多张图组合」，添加至少两张照片，说明各取哪部分。第一次进入会带入刚才添加的参考。组合会重新设计整体布局；要保留某张图的其他部分，请在那张图上用「只改选区」。'),
    el('p', {}, '「保存到效果图文件夹」把图片存到这台 Mac，保存后可点「在访达中显示」。浏览器下载的位置由浏览器决定。'),
    el('p', {}, '未提交的文字会尝试暂存在这台设备的浏览器里。暂存失败会提示风险，可保留当前填写后重试；尚未读出的旧草稿不会自动覆盖。不想在列表里看到的设计，可以在设计页的「更多」里隐藏，列表底部能恢复。'),
    el('p', { class: 'zs-help' }, '效果图用来讨论外观，不能当作尺寸图或加工图；结构、工艺和宗教细节要由专业人员确认。')];
}
function helpMenu() {
  const panelBox = el('div', { class: 'zs-help-panel' });
  const menu = el('details', { class: 'zs-help-menu', ontoggle: () => { if (menu.open) panelBox.replaceChildren(...helpText().filter(Boolean)); } },
    el('summary', { 'aria-label': '使用说明' }, el('span', {}, '使用说明')), panelBox);
  return menu;
}

async function openStudio() {
  const boot = ++studioBoot;
  if (!panel) {
    if (!document.querySelector('link[data-zyb-studio-style]')) {
      const css = document.createElement('link'); css.rel = 'stylesheet'; css.dataset.zybStudioStyle = '1';
      css.href = new URL('./studio.css?v=20.23', import.meta.url).href; document.head.append(css);
    }
    panel = el('dialog', { class: 'zs-studio', 'aria-label': '庄严宝刹设计工作台' },
      el('header', { class: 'zs-header' },
        el('div', { class: 'zs-brand', translate: 'no' }, el('span', { class: 'zs-mark', 'aria-hidden': 'true' }, '庄'), el('div', {}, el('strong', {}, '庄严宝刹'), el('small', {}, '设计工作台'))),
        el('nav', { class: 'zs-nav', 'aria-label': '页面' }),
        el('div', { class: 'zs-header-actions' }, el('span', { class: 'zs-local' }, '正在连接…'),
          el('button', { class: 'zs-btn primary zs-new', type: 'button', 'aria-label': '新建设计', onclick: startNew }, icon('plus'), el('span', {}, '新建设计')),
          helpMenu())),
      el('section', { class: 'zs-content' }),
      el('div', { class: 'zs-notice', role: 'status', 'aria-live': 'polite', hidden: true }));
    document.body.append(panel); panel.addEventListener('close', () => { closeInteriorPhotos(); closeMoreMenu?.(); ++navigation; clearTimeout(pollTimer); });
    const dismissOverlay = () => {
      const help = panel.querySelector('.zs-help-menu');
      const menu = panel.querySelector('.zs-more[open]');
      if (help.open) { help.open = false; help.querySelector('summary').focus(); }
      else if (menu) { closeMoreMenu?.(); menu.querySelector('summary').focus({ preventScroll: true }); }
      else if (renaming != null) { renaming = null; render(); }
      else if (comparing) { comparing = false; render(); }
    };
    panel.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || event.isComposing || event.keyCode === 229) return;
      event.preventDefault(); event.stopPropagation(); dismissOverlay();
    }, true);
    panel.addEventListener('cancel', event => {
      event.preventDefault(); dismissOverlay();
    });
    panel.addEventListener('click', event => {
      const help = panel.querySelector('.zs-help-menu');
      if (help.open && !help.contains(event.target)) help.open = false;
      for (const menu of panel.querySelectorAll('.zs-more[open]')) if (!menu.contains(event.target)) menu.open = false;
    });
    const title = document.querySelector('title');
    const updateTitle = () => { if (panel.open && document.title !== '庄严宝刹 · 设计工作台') document.title = '庄严宝刹 · 设计工作台'; };
    if (title) new MutationObserver(updateTitle).observe(title, { childList: true });
    updateTitle();
    const flushDrafts = () => { clearTimeout(draftTimer); if (panel.open && view) { rememberInputs(); saveDrafts(); } };
    const later = () => { updateActionButtons(); refreshValidationNotice(); clearTimeout(draftTimer); draftTimer = setTimeout(flushDrafts, 400); };
    panel.addEventListener('input', later); panel.addEventListener('change', later);
    window.addEventListener('pagehide', flushDrafts);
    window.addEventListener('beforeunload', event => {
      flushDrafts();
      if (formDraftState().dirty && formDraftState().error || [...editDrafts.values()].some(draft => draft.storageState?.dirty && draft.storageState?.error)) {
        event.preventDefault(); event.returnValue = '';
      }
    });
    window.addEventListener('popstate', () => {
      if (panel.open && status) applyRoute(parseRoute(placeOf(location.search))).catch(error => tell(error.message, true));
    });
  }
  if (!panel.open) panel.showModal();
  document.getElementById('zyb-studio-loading')?.remove();
  if (!status) panel.querySelector('.zs-content').replaceChildren(
    el('div', { class: 'zs-waiting', role: 'status' }, el('span', { class: 'zs-spinner', 'aria-hidden': 'true' }),
      el('p', {}, '正在打开设计与参考资料…')));
  document.title = '庄严宝刹 · 设计工作台';
  try {
    const [connected, , styles] = await Promise.all([api('status'), loadTasks(), api('styles').catch(() => ({ packs: [], unavailable: true }))]);
    if (boot !== studioBoot || !panel.open) return;
    status = connected; stylePacks = styles.packs;
    stylesError = styles.unavailable ? '精选风格目录暂未载入，已保存的参考仍保留。请核对本次参考；需要选用其他风格时，刷新页面重新载入。' : '';
    materialsLoading = true; materialsError = '';
    panel.querySelector('.zs-local').textContent = status.planner_ready && status.image_ready ? '已连接'
      : !status.image_ready && !status.planner_ready ? '暂时不能构思和生成' : !status.image_ready ? '暂时不能生成图片' : '暂时不能构思方案';
    panel.querySelector('.zs-local').dataset.state = status.planner_ready && status.image_ready ? 'ok' : 'partial';
    loadDrafts();
    await applyRoute(parseRoute(placeOf(location.search)));
    void api('materials').then(result => {
      if (boot === studioBoot) materials = result.materials;
    }).catch(() => {
      if (boot === studioBoot) { materials = []; materialsError = '石材样图库暂未载入，已有设计和手动参考不受影响。'; }
    }).finally(() => {
      if (boot === studioBoot) { materialsLoading = false; if (panel.open) { render(); schedule(); } }
    });
  } catch (error) {
    panel.querySelector('.zs-local').textContent = '连接中断'; panel.querySelector('.zs-local').dataset.state = 'off';
    render(); tell(error.message, true);
  }
}

if (document.documentElement.dataset.zybStudio === 'standalone') {
  await openStudio();
} else {
const { app } = await import('../../../scripts/app.js');
app.registerExtension({ name: 'zyb.product.studio',
  commands: [{ id: 'zyb.openStudio', label: '产品设计工作台', function: openStudio }],
  menuCommands: [{ path: ['庄严宝刹'], commands: ['zyb.openStudio'] }],
  async setup() {
    const launch = button('庄严宝刹 · 设计工作台', openStudio, true, false); launch.id = 'zyb-studio-launch';
    launch.style.cssText = 'position:fixed;bottom:18px;left:64px;z-index:1200;padding:10px 18px;border-radius:22px;background:#274b9f;color:white;border:0;cursor:pointer;font:500 14px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif';
    document.body.append(launch);
    if (new URLSearchParams(location.search).get('zyb') === 'studio') await openStudio();
  }
});
}
