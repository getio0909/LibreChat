const prefix = 'zyb.studio.tool-draft.v1.';
const maxBytes = 128 * 1024;

export function toolDraft(kind, identity, validate, storage = () => globalThis.sessionStorage) {
  const key = prefix + [kind, ...identity].map(value => encodeURIComponent(value)).join('.');
  const validKey = /^[a-z-]+$/.test(kind) && identity.every(value => typeof value === 'string' && value.length > 0 && value.length <= 200);
  const failed = error => ({ ok: false, error });
  return {
    key,
    read() {
      if (!validKey) return failed('草稿归属不完整，无法恢复。');
      try {
        const raw = storage().getItem(key);
        if (raw === null) return { ok: true, value: null };
        if (new TextEncoder().encode(raw).length > maxBytes) return failed('草稿内容过大，无法恢复。');
        const record = JSON.parse(raw);
        if (record?.schema !== 1 || record.kind !== kind || !Number.isFinite(record.updated) || !validate(record.value))
          return failed('草稿格式不完整，无法恢复。');
        return { ok: true, value: record.value, updated: record.updated };
      } catch { return failed('浏览器暂时无法读取草稿。'); }
    },
    write(value) {
      if (!validKey) return failed('草稿归属不完整，无法暂存。');
      try {
        const raw = JSON.stringify({ schema: 1, kind, updated: Date.now(), value });
        if (new TextEncoder().encode(raw).length > maxBytes) return failed('草稿内容过大，无法暂存。');
        if (!validate(JSON.parse(raw).value)) return failed('草稿格式不完整，无法暂存。');
        storage().setItem(key, raw);
        return { ok: true };
      } catch { return failed('浏览器暂时无法暂存草稿。'); }
    },
    clear() {
      if (!validKey) return failed('草稿归属不完整，无法清除。');
      try { storage().removeItem(key); return { ok: true }; }
      catch { return failed('浏览器暂时无法清除草稿。'); }
    },
  };
}
