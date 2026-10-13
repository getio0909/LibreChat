import { inflateSync } from 'node:zlib';
import { PDFArray, PDFDict, PDFDocument, PDFName, PDFRawStream, PDFObject, PDFNull } from 'pdf-lib';
import { ToolFailure } from './config.ts';

function references(content: Uint8Array): Set<PDFName> {
  const text = Buffer.from(content.buffer, content.byteOffset, content.byteLength).toString(
    'latin1',
  );
  const names = new Set<PDFName>();
  let name: PDFName | undefined;
  let nested = 0;
  let i = 0;
  const whitespace = (char: string) => /[\x00\t\n\f\r ]/.test(char);
  const delimiter = (char: string) => whitespace(char) || '()<>[]/%'.includes(char);
  while (i < text.length) {
    const char = text[i];
    if (whitespace(char)) {
      i++;
      continue;
    }
    if (char === '%') {
      while (i < text.length && !/[\r\n]/.test(text[i])) i++;
      continue;
    }
    if (char === '(') {
      let depth = 1;
      i++;
      while (i < text.length && depth) {
        if (text[i] === '\\') {
          i += 2;
          continue;
        }
        if (text[i] === '(') depth++;
        if (text[i] === ')') depth--;
        i++;
      }
      if (depth) throw new ToolFailure('INVALID_PDF');
      name = undefined;
      continue;
    }
    if (char === '<' && text[i + 1] !== '<') {
      const end = text.indexOf('>', i + 1);
      if (end < 0) throw new ToolFailure('INVALID_PDF');
      i = end + 1;
      name = undefined;
      continue;
    }
    if (char === '[' || (char === '<' && text[i + 1] === '<')) {
      nested++;
      i += char === '[' ? 1 : 2;
      name = undefined;
      continue;
    }
    if (char === ']' || (char === '>' && text[i + 1] === '>')) {
      nested--;
      i += char === ']' ? 1 : 2;
      name = undefined;
      if (nested < 0) throw new ToolFailure('INVALID_PDF');
      continue;
    }
    const start = i++;
    while (i < text.length && !delimiter(text[i])) i++;
    const token = text.slice(start, i);
    if (nested) continue;
    if (token.startsWith('/')) {
      name = PDFName.of(
        token
          .slice(1)
          .replace(/#([a-f0-9]{2})/gi, (_, hex: string) => String.fromCharCode(parseInt(hex, 16))),
      );
      continue;
    }
    if (token === 'BI') throw new ToolFailure('PDF_INLINE_IMAGE_UNSUPPORTED');
    if (token === 'Do' && name) names.add(name);
    if (!/^[+-]?(?:\d*\.?\d+)$/.test(token)) name = undefined;
  }
  if (nested) throw new ToolFailure('INVALID_PDF');
  return names;
}

export function pruneResources(pdf: PDFDocument, limit: number): void {
  const used = new Map<PDFDict, Set<PDFName>>();
  const parsed = new Map<PDFRawStream, Set<PDFName>>();
  const scanned = new Map<PDFObject, Set<PDFDict>>();
  let expanded = 0;
  const streamNames = (stream: PDFRawStream) => {
    const cached = parsed.get(stream);
    if (cached) return cached;
    let bytes = stream.getContents();
    const filter = stream.dict.lookup(PDFName.of('Filter'));
    if (filter) {
      const filters = filter instanceof PDFArray ? filter.asArray() : [filter];
      const params = stream.dict.lookup(PDFName.of('DecodeParms'));
      const parameter = params instanceof PDFArray ? pdf.context.lookup(params.get(0)) : params;
      if (
        filters.length !== 1 ||
        pdf.context.lookup(filters[0]) !== PDFName.of('FlateDecode') ||
        (params instanceof PDFArray && params.size() !== filters.length) ||
        (parameter != null && parameter !== PDFNull)
      )
        throw new ToolFailure('PDF_CONTENT_FILTER_UNSUPPORTED');
      try {
        bytes = inflateSync(bytes, { maxOutputLength: Math.max(1, limit - expanded) });
      } catch {
        throw new ToolFailure('FILE_TOO_LARGE');
      }
    }
    expanded += bytes.byteLength;
    if (expanded > limit) throw new ToolFailure('FILE_TOO_LARGE');
    const result = references(bytes);
    parsed.set(stream, result);
    return result;
  };
  const scan = (content: PDFObject | undefined, resources: PDFDict | undefined): void => {
    if (!content || !resources) return;
    const resolved = pdf.context.lookup(content);
    if (!resolved) return;
    const scopes = scanned.get(resolved) ?? new Set<PDFDict>();
    if (scopes.has(resources)) return;
    scopes.add(resources);
    scanned.set(resolved, scopes);
    if (resolved instanceof PDFArray) {
      for (const item of resolved.asArray()) scan(item, resources);
      return;
    }
    if (!(resolved instanceof PDFRawStream)) throw new ToolFailure('INVALID_PDF');
    const names = used.get(resources) ?? new Set<PDFName>();
    used.set(resources, names);
    const xobjects = resources.lookup(PDFName.of('XObject'));
    for (const name of streamNames(resolved)) {
      names.add(name);
      const object = xobjects instanceof PDFDict ? xobjects.lookup(name) : undefined;
      if (
        object instanceof PDFRawStream &&
        object.dict.lookup(PDFName.of('Subtype')) === PDFName.of('Form')
      ) {
        const own = object.dict.lookup(PDFName.of('Resources'));
        scan(object, own instanceof PDFDict ? own : resources);
      }
    }
    // 图案和软蒙版可能间接调用 XObject；Type3 字体需要生产端转换以移除未使用字形。
    for (const key of ['Pattern', 'ExtGState', 'Font']) {
      const entries = resources.lookup(PDFName.of(key));
      if (!(entries instanceof PDFDict)) continue;
      for (const [, ref] of entries.entries()) {
        const object = pdf.context.lookup(ref);
        if (object instanceof PDFRawStream) {
          const own = object.dict.lookup(PDFName.of('Resources'));
          scan(object, own instanceof PDFDict ? own : resources);
        } else if (object instanceof PDFDict) {
          if (object.lookup(PDFName.of('Subtype')) === PDFName.of('Type3'))
            throw new ToolFailure('PDF_TYPE3_FONT_UNSUPPORTED');
          const mask = object.lookup(PDFName.of('SMask'));
          if (mask instanceof PDFDict) {
            const form = mask.lookup(PDFName.of('G'));
            if (form instanceof PDFRawStream) {
              const own = form.dict.lookup(PDFName.of('Resources'));
              scan(form, own instanceof PDFDict ? own : resources);
            }
          }
        }
      }
    }
  };
  for (const page of pdf.getPages()) {
    const resources = page.node.Resources();
    if (resources) {
      if (!used.has(resources)) used.set(resources, new Set());
      scan(page.node.Contents(), resources);
    }
  }
  for (const [resources, names] of used) {
    const objects = resources.lookup(PDFName.of('XObject'));
    if (objects instanceof PDFDict)
      for (const [name] of objects.entries()) if (!names.has(name)) objects.delete(name);
  }
}
