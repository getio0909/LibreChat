import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const plugin = process.env.ZYB_PLUGIN_PATH || (process.platform === 'darwin'
  ? '/Users/ipd/workspace/ComfyUI/custom_nodes/zyb_obsidian_design'
  : fileURLToPath(new URL('./zyb_obsidian_design', import.meta.url)));
const source = fs.readFileSync(path.join(plugin, 'web/studio.js'), 'utf8');
const asyncFunctions = source.slice(source.indexOf('async function loadTasks('), source.indexOf('function field('));
const newTaskFunction = source.slice(source.indexOf('function newTask('), source.indexOf('function newForm('));
const searchFunction = source.slice(source.indexOf('function matchesProject('), source.indexOf('function renderProjectList('));
const reviewFunctions = source.slice(source.indexOf('function reviewedVersion('), source.indexOf('function reviewView('));
const rememberFunction = source.slice(source.indexOf('function rememberInputs('), source.indexOf('function restoreInputs('));
const fixture = (id, revision = 1, running = false) => ({ id, revision, references: [], concepts: [],
  job: running ? { status: 'running' } : null });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

function harness(handler) {
  const timers = new Map(), notices = [], calls = [];
  let timerId = 0;
  const state = vm.createContext({
    panel: { open: true }, task: fixture('A', 10, true), tasks: [], refs: [], picked: new Map(),
    stage: 2, planning: false, navigation: 0, listRequest: 0, pollTimer: 0, drafts: new Map(), draftKey: null, imageSelection: new Map(), pendingUploads: new Map(),
    document: { hidden: false }, window: { confirm: () => false }, localStorage: { setItem() {} }, render() {},
    tell: message => notices.push(message),
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id),
    api: async (path, body) => {
      calls.push({ path, body });
      if (path === 'tasks') return { tasks: [] };
      return handler(path, body);
    },
  });
  vm.runInContext("const busy = () => planning || task?.job?.status === 'running';\n" + asyncFunctions + newTaskFunction, state);
  return { state, calls, notices, poll: () => {
    state.schedule();
    const callback = timers.get(state.pollTimer);
    timers.delete(state.pollTimer);
    return callback();
  } };
}

test('project search covers title, material and purpose without case sensitivity', () => {
  const context = vm.createContext({});
  vm.runInContext(searchFunction, context);
  const task = { brief: { title: 'Studio柜体', goal: '入口收纳', materials: '浅色木材', category: '家具与佛龛' } };
  assert.equal(context.matchesProject(task, 'studio 木材'), true);
  assert.equal(context.matchesProject(task, '入口'), true);
  assert.equal(context.matchesProject(task, '不存在'), false);
  assert.equal(context.matchesProject(task, '   '), true);
});

test('starting another task clears only the unsubmitted new-image choice', () => {
  const h = harness(() => fixture('A'));
  h.state.pendingUploads.set('new', { file: 'new' });
  h.state.pendingUploads.set('A', { file: 'existing' });
  h.state.newTask();
  assert.equal(h.state.pendingUploads.has('new'), false);
  assert.equal(h.state.pendingUploads.has('A'), true);
});

test('switching to another image does not inherit a passed review', () => {
  const context = vm.createContext({ task: { id: 'A', selected: 'C',
    images: [{ id: 'old', concept_id: 'C' }, { id: 'new', concept_id: 'C' }],
    review: { image_id: 'new', reviewer: 'reviewer', design: '通过', culture: '通过', process: '通过' } },
    imageSelection: new Map([['A', 'old']]) });
  vm.runInContext(reviewFunctions, context);
  assert.equal(context.reviewedVersion().id, 'old');
  assert.equal(context.reviewValues(context.reviewedVersion()).design, '待核验');
  context.imageSelection.set('A', 'new');
  assert.equal(context.reviewValues(context.reviewedVersion()).design, '通过');
});

test('legacy review applies only to the latest image of its direction', () => {
  const context = vm.createContext({ task: { id: 'A', selected: 'C',
    images: [{ id: 'old', concept_id: 'C' }, { id: 'new', concept_id: 'C' }, { id: 'other', concept_id: 'D' }],
    review: { reviewer: 'reviewer', design: '通过', culture: '待核验', process: '待核验' } },
    imageSelection: new Map([['A', 'old']]) });
  vm.runInContext(reviewFunctions, context);
  assert.equal(context.reviewValues(context.reviewedVersion()).design, '待核验');
  context.imageSelection.set('A', 'new');
  assert.equal(context.reviewValues(context.reviewedVersion()).design, '通过');
});

test('drafts never restore approvals or cloud consent', () => {
  const nodes = ['reviewer', 'change', 'image_id', 'design', 'culture', 'process', 'cloud_consent', 'vision_consent']
    .map(name => ({ name, value: 'value', type: 'text', form: { dataset: { review: '1' } } }));
  nodes.push({name:'culture', value:'draft culture', type:'text'}, {name:'process', value:'draft process', type:'text'});
  const context = vm.createContext({ view: { querySelectorAll: () => nodes }, draftKey: 'A:4', drafts: new Map(),
    serverOwnedInputs: new Set(['image_id', 'cloud_consent', 'vision_consent']) });
  vm.runInContext(rememberFunction, context);
  context.rememberInputs();
  assert.deepEqual(Array.from(context.drafts.get('A:4'), row => row[0]), ['reviewer', 'change', 'culture', 'process']);
});

test('review defaults to its recorded version unless an image is explicitly selected', () => {
  const context = vm.createContext({ task: { id: 'A', selected: 'C',
    images: [{ id: 'old', concept_id: 'C' }, { id: 'new', concept_id: 'C' }],
    review: { image_id: 'old', reviewer: 'reviewer', design: '通过' } }, imageSelection: new Map() });
  vm.runInContext(reviewFunctions, context);
  assert.equal(context.reviewedVersion().id, 'old');
  context.imageSelection.set('A', 'new');
  assert.equal(context.reviewedVersion().id, 'new');
  assert.equal(context.reviewValues(context.reviewedVersion()).design, '待核验');
});

test('late poll cannot replace a newly selected task', async () => {
  const pending = deferred();
  const h = harness(path => path === 'tasks/A' ? pending.promise : fixture('B', 2));
  const request = h.poll();
  await h.state.chooseTask('B');
  pending.resolve(fixture('A', 11, true));
  await request;
  assert.equal(h.state.task.id, 'B');
  assert.equal(h.state.stage, 1);
});

test('latest task selection wins out-of-order responses', async () => {
  const pending = deferred();
  const h = harness(path => path === 'tasks/A' ? pending.promise : fixture('B', 2));
  const first = h.state.chooseTask('A');
  assert.equal(await h.state.chooseTask('B'), true);
  pending.resolve(fixture('A', 11));
  assert.equal(await first, false);
  assert.equal(h.state.task.id, 'B');
});

test('reopening a task defaults to its newest image instead of an old edit source', async () => {
  const h = harness(() => fixture('A', 12));
  h.state.imageSelection.set('A', 'original');
  await h.state.chooseTask('A');
  assert.equal(h.state.imageSelection.has('A'), false);
});

test('late mutation response does not navigate or change the new task', async () => {
  const pending = deferred();
  const h = harness(path => path === 'tasks/A/select' ? pending.promise : fixture('B'));
  const request = h.state.action('select', { concept_id: 'example' });
  await h.state.chooseTask('B');
  pending.resolve(fixture('A', 11));
  assert.equal(await request, false);
  assert.equal(h.state.task.id, 'B');
});

test('same-task response cannot regress a newer revision', async () => {
  const pending = deferred();
  const h = harness(() => pending.promise);
  const request = h.state.action('review');
  h.state.task = fixture('A', 12);
  pending.resolve(fixture('A', 11));
  assert.equal(await request, true);
  assert.equal(h.state.task.revision, 12);
});

test('uncertain paid request requires explicit acknowledgment', async () => {
  const h = harness(() => fixture('A', 11));
  h.state.task.job = { status: 'uncertain' };
  assert.equal(await h.state.action('generate', { concept_ids: ['test'] }), false);
  assert.equal(h.calls.length, 0);
});

test('confirmed uncertain request carries acknowledgment', async () => {
  const h = harness(() => fixture('A', 11));
  h.state.task.job = { status: 'uncertain' };
  h.state.window.confirm = () => true;
  assert.equal(await h.state.action('refine'), true);
  assert.equal(h.calls[0].body.acknowledge_uncertain, true);
});

test('new task invalidates an in-flight poll', async () => {
  const pending = deferred();
  const h = harness(() => pending.promise);
  const request = h.poll();
  h.state.newTask();
  pending.resolve(fixture('A', 11, true));
  await request;
  assert.equal(h.state.task, null);
  assert.equal(h.state.stage, 0);
});

test('closed workbench ignores a late poll', async () => {
  const pending = deferred();
  const h = harness(() => pending.promise);
  const request = h.poll();
  h.state.panel.open = false;
  ++h.state.navigation;
  pending.resolve(fixture('A', 11, true));
  await request;
  assert.equal(h.state.task.revision, 10);
});

test('old errors do not show on the new task', async () => {
  const pending = deferred();
  const h = harness(path => path === 'tasks/A' ? pending.promise : fixture('B'));
  const request = h.poll();
  await h.state.chooseTask('B');
  pending.reject(Error('test disconnect'));
  await request;
  assert.deepEqual(h.notices, []);
});

test('leaving during reference save cannot launch a plan for another task', async () => {
  const pending = deferred();
  const h = harness(path => path === 'tasks/A/references' ? pending.promise : fixture('B'));
  h.state.task.job = null;
  const request = h.state.startPlan(false);
  await h.state.chooseTask('B');
  pending.resolve(fixture('A', 11));
  await request;
  assert.equal(h.state.task.id, 'B');
  assert.equal(h.state.planning, false);
  assert(!h.calls.some(call => call.path.endsWith('/plan')));
});

test('normal no-reference plan still saves and launches on one task', async () => {
  const h = harness(path => fixture('A', path.endsWith('/plan') ? 12 : 11, path.endsWith('/plan')));
  h.state.task.job = null;
  await h.state.startPlan(false);
  assert.equal(h.state.task.id, 'A');
  assert.equal(h.state.task.job.status, 'running');
  assert.equal(h.state.stage, 2);
  assert.equal(h.state.planning, false);
  assert.deepEqual(h.calls.filter(call => call.body).map(call => call.path), ['tasks/A/references', 'tasks/A/plan']);
});

test('newly returned image selects the new version instead of a stale edit source', async () => {
  const next = {...fixture('A', 11), images: [{id: 'old'}, {id: 'new'}], job: {status: 'done', action: 'refine'}};
  const h = harness(() => next);
  h.state.task.images = [{id: 'old'}];
  h.state.imageSelection.set('A', 'old');
  await h.poll();
  assert.equal(h.state.imageSelection.has('A'), false);
  assert.equal(h.state.task.images.at(-1).id, 'new');
});

test('failed image request retains the selected source version', async () => {
  const h = harness(() => ({...fixture('A', 11), images: [{id: 'old'}], job: {status: 'failed', action: 'refine'}}));
  h.state.task.images = [{id: 'old'}];
  h.state.imageSelection.set('A', 'old');
  await h.poll();
  assert.equal(h.state.imageSelection.get('A'), 'old');
});
