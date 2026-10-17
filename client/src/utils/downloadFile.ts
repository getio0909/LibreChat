import { FileSources, FileContext } from 'librechat-data-provider';
import type { TFile } from 'librechat-data-provider';

const blobDownloadFilenames = new Map<string, string>();
const GENERATED_IMAGE_EXTENSIONS: Record<string, string> = {
  'image/jpeg': '.jpg',
  'image/webp': '.webp',
  'image/gif': '.gif',
};

export const isHttpDownloadTarget = (target?: string | null): boolean =>
  /^https?:\/\//i.test(target ?? '');

// 仅依据 File 持久化的产图上下文生成中立名称，不从用户可控文件名猜测生产者
export function formatGeneratedImageFilename(
  file?: Pick<TFile, 'filename'> & Partial<Pick<TFile, 'file_id' | 'context' | 'type'>>,
  localize?: (
    phraseKey: 'com_ui_generated_image_filename',
    options?: Record<string, string | number>,
  ) => string,
): string {
  const raw = (file?.filename ?? '').trim();
  if (!raw) {
    return '';
  }
  if (file?.context !== FileContext.image_generation) {
    return raw;
  }
  const shortId = (file.file_id ?? '').replace(/[^A-Za-z0-9]/g, '').slice(0, 8) || 'image';
  const ext = GENERATED_IMAGE_EXTENSIONS[file.type ?? ''] ?? '.png';
  const suffix = `${shortId}${ext}`;
  if (typeof localize === 'function') {
    const localized = localize('com_ui_generated_image_filename', { 0: suffix });
    if (localized && localized !== 'com_ui_generated_image_filename') {
      return localized;
    }
  }
  return `generated-image-${suffix}`;
}

export function getDownloadFilename(
  fileName: string,
  fileId?: string,
  fileSource?: string | null,
  fileContext?: TFile['context'],
  fileType?: string,
): string {
  const sanitizedFileName = formatGeneratedImageFilename({
    filename: fileName,
    file_id: fileId,
    context: fileContext,
    type: fileType,
  });
  const filename = sanitizedFileName || fileId || 'download';
  if (fileSource !== FileSources.text || filename.toLowerCase().endsWith('.txt')) {
    return filename;
  }
  return `${filename}.txt`;
}

export function registerDownloadFilename(target: string, filename: string): void {
  if (target.startsWith('blob:')) {
    blobDownloadFilenames.set(target, filename);
  }
}

export function unregisterDownloadFilename(target: string): void {
  blobDownloadFilenames.delete(target);
}

/**
 * Maps a fenced-block language hint to a file extension. Used to name
 * downloads of chat code blocks (`code.<ext>`). Only languages whose common
 * name differs from their extension need an entry; hints that already look
 * like an extension (`py`, `ts`, `json`) pass through unchanged.
 */
const LANGUAGE_TO_EXTENSION: Record<string, string> = {
  javascript: 'js',
  node: 'js',
  nodejs: 'js',
  typescript: 'ts',
  python: 'py',
  python3: 'py',
  golang: 'go',
  ruby: 'rb',
  perl: 'pl',
  rust: 'rs',
  'c++': 'cpp',
  csharp: 'cs',
  'c#': 'cs',
  objectivec: 'm',
  kotlin: 'kt',
  julia: 'jl',
  elixir: 'ex',
  erlang: 'erl',
  haskell: 'hs',
  clojure: 'clj',
  fsharp: 'fs',
  'f#': 'fs',
  bash: 'sh',
  shell: 'sh',
  zsh: 'sh',
  powershell: 'ps1',
  batch: 'bat',
  graphql: 'graphql',
  protobuf: 'proto',
  markdown: 'md',
  yaml: 'yaml',
  yml: 'yaml',
  plaintext: 'txt',
  text: 'txt',
};

/**
 * Builds a download filename for a chat code block from its fenced-block
 * language hint. Unknown-but-extension-like hints are used verbatim so a
 * ```` ```toml ```` block still downloads as `code.toml`; anything else
 * falls back to `code.txt`.
 */
export function getCodeBlockFilename(lang?: string | null): string {
  const hint = (lang ?? '').trim().toLowerCase();
  const mapped = Object.prototype.hasOwnProperty.call(LANGUAGE_TO_EXTENSION, hint)
    ? LANGUAGE_TO_EXTENSION[hint]
    : undefined;
  const extension = mapped ?? (/^[a-z0-9]{1,11}$/.test(hint) ? hint : 'txt');
  return `code.${extension}`;
}

export function triggerDownload(target: string, filename: string): void {
  const isBlob = target.startsWith('blob:');
  const link = document.createElement('a');
  link.href = target;
  link.setAttribute('download', blobDownloadFilenames.get(target) ?? filename);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  if (isBlob) {
    setTimeout(() => {
      unregisterDownloadFilename(target);
      URL.revokeObjectURL(target);
    }, 1000);
  }
}
