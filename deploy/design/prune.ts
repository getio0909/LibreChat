import { inflateSync } from 'node:zlib';
import { PDFArray, PDFDict, PDFDocument, PDFName, PDFRawStream, PDFObject, PDFNull } from 'pdf-lib';
import { ToolFailure } from './config.ts';

type Resource =
  | 'XObject'
  | 'Pattern'
  | 'ExtGState'
  | 'Font'
  | 'Shading'
  | 'ColorSpace'
  | 'Properties';
type References = Map<Resource, Set<PDFName>>;
const operators: Record<string, Resource> = {
  Do: 'XObject',
  SCN: 'Pattern',
  scn: 'Pattern',
  gs: 'ExtGState',
  Tf: 'Font',
  sh: 'Shading',
  cs: 'ColorSpace',
  CS: 'ColorSpace',
  BDC: 'Properties',
  DP: 'Properties',
};
const resourceKinds: Resource[] = [
  'XObject',
  'Pattern',
  'ExtGState',
  'Font',
  'Shading',
  'ColorSpace',
  'Properties',
];
function emptyReferences(): References {
  return new Map(resourceKinds.map((key) => [key, new Set<PDFName>()]));
}

function references(content: Uint8Array): References {
  const text = Buffer.from(content.buffer, content.byteOffset, content.byteLength).toString(
    'latin1',
  );
  const names = emptyReferences();
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
    const kind = Object.hasOwn(operators, token) ? operators[token] : undefined;
    if (kind && name) names.get(kind)!.add(name);
    if (!/^[+-]?(?:\d*\.?\d+)$/.test(token)) name = undefined;
  }
  if (nested) throw new ToolFailure('INVALID_PDF');
  return names;
}

export function pruneResources(pdf: PDFDocument, limit: number): void {
  const used = new Map<PDFDict, References>();
  const decoded = new Map<PDFRawStream, Uint8Array>();
  const parsed = new Map<PDFObject, References>();
  const scanned = new Map<PDFObject, Set<PDFDict>>();
  const colors = new Map<PDFObject, Set<PDFDict>>();
  let expanded = 0;
  const streamBytes = (stream: PDFRawStream) => {
    const cached = decoded.get(stream);
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
    decoded.set(stream, bytes);
    return bytes;
  };
  const markColor = (value: PDFObject | undefined, resources: PDFDict): void => {
    const resolved = pdf.context.lookup(value);
    if (!resolved) return;
    const scopes = colors.get(resolved) ?? new Set<PDFDict>();
    if (scopes.has(resources)) return;
    scopes.add(resources);
    colors.set(resolved, scopes);
    if (resolved instanceof PDFName) {
      const entries = resources.lookup(PDFName.of('ColorSpace'));
      if (!(entries instanceof PDFDict) || !entries.has(resolved)) return;
      const names = used.get(resources) ?? emptyReferences();
      used.set(resources, names);
      names.get('ColorSpace')!.add(resolved);
      markColor(entries.lookup(resolved), resources);
      return;
    }
    if (!(resolved instanceof PDFArray)) return;
    const family = resolved.lookup(0);
    if (family === PDFName.of('Indexed') || family === PDFName.of('Pattern'))
      markColor(resolved.get(1), resources);
    else if (family === PDFName.of('Separation') || family === PDFName.of('DeviceN'))
      markColor(resolved.get(2), resources);
    else if (family === PDFName.of('ICCBased')) {
      const profile = resolved.lookup(1);
      if (profile instanceof PDFRawStream)
        markColor(profile.dict.lookup(PDFName.of('Alternate')), resources);
    }
  };
  const preserveColors = (dict: PDFDict, resources: PDFDict): void => {
    markColor(dict.lookup(PDFName.of('ColorSpace')), resources);
    const group = dict.lookup(PDFName.of('Group'));
    if (group instanceof PDFDict) markColor(group.lookup(PDFName.of('CS')), resources);
    const shading = dict.lookup(PDFName.of('Shading'));
    if (shading instanceof PDFDict) markColor(shading.lookup(PDFName.of('ColorSpace')), resources);
  };
  const scan = (content: PDFObject | undefined, resources: PDFDict | undefined): void => {
    if (!content || !resources) return;
    const resolved = pdf.context.lookup(content);
    if (!resolved) return;
    const scopes = scanned.get(resolved) ?? new Set<PDFDict>();
    if (scopes.has(resources)) return;
    scopes.add(resources);
    scanned.set(resolved, scopes);
    let calls = parsed.get(resolved);
    if (!calls) {
      const streams = resolved instanceof PDFArray ? resolved.asArray() : [resolved];
      const chunks = streams.map((item) => {
        const stream = pdf.context.lookup(item);
        if (!(stream instanceof PDFRawStream)) throw new ToolFailure('INVALID_PDF');
        return streamBytes(stream);
      });
      const size = chunks.reduce((total, chunk) => total + chunk.byteLength, 0);
      if (size > limit) throw new ToolFailure('FILE_TOO_LARGE');
      // 页面内容数组是一条连续指令流，名称、字符串和操作符都可能跨越流边界。
      calls = references(chunks.length === 1 ? chunks[0] : Buffer.concat(chunks, size));
      parsed.set(resolved, calls);
    }
    const names = used.get(resources) ?? emptyReferences();
    used.set(resources, names);
    if (resolved instanceof PDFRawStream) preserveColors(resolved.dict, resources);
    // 默认色彩空间和图像、着色、透明组的颜色引用不需要显式 cs/CS 指令。
    for (const key of ['DefaultGray', 'DefaultRGB', 'DefaultCMYK'])
      markColor(PDFName.of(key), resources);
    for (const key of resourceKinds) {
      const entries = resources.lookup(PDFName.of(key));
      for (const name of calls.get(key)!) {
        names.get(key)!.add(name);
        if (key === 'ColorSpace') {
          markColor(name, resources);
          continue;
        }
        const object = entries instanceof PDFDict ? entries.lookup(name) : undefined;
        const dict = object instanceof PDFRawStream ? object.dict : object;
        if (dict instanceof PDFDict) {
          const own = dict.lookup(PDFName.of('Resources'));
          preserveColors(dict, own instanceof PDFDict ? own : resources);
        }
        if (
          object instanceof PDFRawStream &&
          (key === 'Pattern' ||
            (key === 'XObject' && object.dict.lookup(PDFName.of('Subtype')) === PDFName.of('Form')))
        ) {
          const own = object.dict.lookup(PDFName.of('Resources'));
          scan(object, own instanceof PDFDict ? own : resources);
        } else if (object instanceof PDFDict) {
          if (key === 'Font' && object.lookup(PDFName.of('Subtype')) === PDFName.of('Type3'))
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
      if (!used.has(resources)) used.set(resources, emptyReferences());
      preserveColors(page.node, resources);
      scan(page.node.Contents(), resources);
    }
  }
  for (const [resources, names] of used) {
    for (const key of resourceKinds) {
      const objects = resources.lookup(PDFName.of(key));
      if (objects instanceof PDFDict)
        for (const [name] of objects.entries())
          if (!names.get(key)!.has(name)) objects.delete(name);
    }
  }
}
