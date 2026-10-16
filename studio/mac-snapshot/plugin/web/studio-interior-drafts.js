const databaseName = 'zyb.studio.interior-files.v1';
const storeName = 'drafts';
const tabKey = 'zyb.studio.interior-tab.v1';
const completedKey = tabKey + '.completed';

export function interiorDraftValid(value) {
  const file = value => typeof File !== 'undefined' && value instanceof File && value.size > 0;
  return Boolean(value && typeof value === 'object' && !Array.isArray(value)
    && (value.plan === null || file(value.plan))
    && typeof value.styleId === 'string' && value.styleId.length <= 200
    && typeof value.styleName === 'string' && value.styleName.length <= 200
    && typeof value.goal === 'string' && value.goal.length <= 600
    && Array.isArray(value.extras) && value.extras.length <= 9
    && value.extras.every(row => row && file(row.file) && ['style', 'material', 'object'].includes(row.use)
      && (row.target === undefined || typeof row.target === 'string' && row.target.length <= 160 && !row.target.includes('\x00'))));
}

export function interiorDraftStore({ factory = () => globalThis.indexedDB,
  session = () => globalThis.sessionStorage,
  token = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('') } = {}) {
  let connection, key, revision, loaded = false, completedInMemory = null, queue = Promise.resolve(), pending = 0, error = '';
  const failure = message => ({ ok: false, error: message });
  function completedRevision() {
    if (completedInMemory?.key === key) return completedInMemory;
    try {
      const receipt = JSON.parse(session().getItem(completedKey));
      return receipt?.key === key && (receipt.revision === null || typeof receipt.revision === 'string') ? receipt : null;
    } catch { return null; }
  }
  function forgetCompleted(previousRevision) {
    if (completedInMemory?.revision === previousRevision) completedInMemory = null;
    try { if (completedRevision()?.revision === previousRevision) session().removeItem(completedKey); } catch {}
  }
  async function open() {
    if (!key) {
      const storage = session();
      key = storage.getItem(tabKey);
      if (!key) { key = token(); storage.setItem(tabKey, key); }
      if (!/^[a-zA-Z0-9-]{1,100}$/.test(key)) throw Error('invalid draft identity');
    }
    if (!connection) connection = new Promise((resolve, reject) => {
      const request = factory().open(databaseName, 1);
      let blocked = false;
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(storeName)) request.result.createObjectStore(storeName);
      };
      request.onerror = () => reject(request.error);
      request.onblocked = () => { blocked = true; reject(Error('draft database blocked')); };
      request.onsuccess = () => {
        if (blocked) { request.result.close(); return; }
        request.result.onversionchange = () => { request.result.close(); connection = null; loaded = false; };
        resolve(request.result);
      };
    }).catch(problem => { connection = null; throw problem; });
    return connection;
  }
  function operation(mode, value, expectedRevision) {
    pending++;
    const work = queue.then(async () => {
      let receiptSaved = false;
      if (mode === 'complete' && loaded && key) {
        completedInMemory = { key, revision };
        try { session().setItem(completedKey, JSON.stringify(completedInMemory)); receiptSaved = true; } catch {}
      }
      const unavailable = message => receiptSaved ? { ok: true, revision, cleanupPending: true } : failure(message);
      try {
        const database = await open();
        if (mode !== 'read' && !loaded) return unavailable('草稿暂未保存到浏览器，当前选择仍在本页。');
        return await new Promise(resolve => {
          const transaction = database.transaction(storeName, mode === 'read' ? 'readonly' : 'readwrite');
          const store = transaction.objectStore(storeName), request = store.get(key);
          let result;
          transaction.onabort = () => resolve(unavailable('草稿暂未保存到浏览器，当前选择仍在本页。'));
          transaction.onerror = () => {};
          transaction.oncomplete = () => {
            if (result?.ok) {
              if (mode !== 'read' && !result.cleanupPending) forgetCompleted(revision);
              revision = result.revision; loaded = true;
            }
            resolve(result || failure('浏览器暂时无法读取草稿。'));
          };
          request.onsuccess = () => {
            try {
              const previous = request.result;
              if (previous !== undefined && (previous?.schema !== 1 || typeof previous.revision !== 'string'
                || !Number.isFinite(previous.updated) || previous.value !== null && !interiorDraftValid(previous.value))) {
                result = failure('旧草稿暂时无法读取，浏览器中的记录没有改动。'); return;
              }
              if (mode === 'read') {
                const consumed = Boolean(previous?.value && completedRevision()?.revision === previous.revision);
                result = { ok: true, value: consumed ? null : previous?.value || null, revision: previous?.revision || null,
                  ...(consumed ? { consumed: true } : {}) }; return;
              }
              if ((previous?.revision || null) !== revision || expectedRevision !== undefined && (previous?.revision || null) !== expectedRevision) {
                result = { ...unavailable('另一个页面更新了草稿，当前选择仍保留在本页。'), conflict: true }; return;
              }
              const nextRevision = token();
              store.put({ schema: 1, revision: nextRevision, updated: Date.now(), value }, key);
              result = { ok: true, revision: nextRevision };
            } catch { transaction.abort(); }
          };
        });
      } catch { return unavailable(mode === 'read' ? '浏览器暂时无法恢复草稿。' : '草稿暂未保存到浏览器，当前选择仍在本页。'); }
    });
    queue = work.then(result => { pending--; error = result.ok ? '' : result.error; return result; });
    return queue;
  }
  return {
    async read() {
      const result = await operation('read');
      if (result.consumed) {
        const previousRevision = result.revision, cleared = await operation('clear', null, result.revision);
        if (cleared.ok) { forgetCompleted(previousRevision); return { ...result, revision: cleared.revision }; }
        if (cleared.conflict) return operation('read');
        return { ...result, cleanupPending: true };
      }
      return result;
    },
    write(value) {
      if (!interiorDraftValid(value)) return Promise.resolve(failure('草稿内容不完整，当前选择仍在本页。'));
      const snapshot = { ...value, extras: value.extras.map(row => ({ file: row.file, use: row.use,
        ...(row.target !== undefined ? { target: row.target } : {}) })) };
      return operation('write', snapshot);
    },
    clear: () => operation('clear', null),
    complete: () => operation('complete', null),
    get pending() { return pending; },
    get error() { return error; },
  };
}
