import { SaxesParser } from 'saxes';
import { ToolFailure } from './config.ts';
export function selfContainedSvg(
  bytes: Uint8Array,
  paper?: { widthMm: number; heightMm: number },
): void {
  const parser = new SaxesParser({ xmlns: true });
  let source: string;
  try {
    source = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    throw new ToolFailure('SVG_ENCODING_UNSUPPORTED');
  }
  if (source.includes('\0')) throw new ToolFailure('SVG_ENCODING_UNSUPPORTED');
  let root = true;
  let styleDepth = 0;
  let style = '';
  const unsupported = () => {
    throw new ToolFailure('SVG_NOT_SELF_CONTAINED');
  };
  const reference = (raw: string) => {
    const value = raw.trim();
    if (/^#[^\s'"]*$/.test(value)) return;
    const data = /^data:image\/(png|jpeg|webp);base64,([a-z0-9+/]*={0,2})$/i.exec(value);
    if (!data) return unsupported();
    const image = Buffer.from(data[2], 'base64');
    const raster =
      data[1].toLowerCase() === 'png'
        ? image.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))
        : data[1].toLowerCase() === 'jpeg'
          ? image.subarray(0, 3).equals(Buffer.from([255, 216, 255]))
          : image.subarray(0, 4).toString() === 'RIFF' &&
            image.subarray(8, 12).toString() === 'WEBP';
    if (!raster) unsupported();
  };
  const checkCss = (value: string) => {
    const css = value.replace(/\/\*[\s\S]*?\*\//g, '');
    // 外部资源无法在无基准路径的渲染中可靠交付，拒绝而不是静默缺图。
    if (/@import|\\/i.test(css)) unsupported();
    for (const match of css.matchAll(/url\s*\(([^)]*)\)?/gi)) {
      if (!match[0].endsWith(')')) unsupported();
      const target = match[1].trim();
      const unquoted =
        (target.startsWith('"') && target.endsWith('"')) ||
        (target.startsWith("'") && target.endsWith("'"))
          ? target.slice(1, -1)
          : target;
      if (/['"]/.test(unquoted)) unsupported();
      reference(unquoted);
    }
  };
  parser.on('error', () => {
    throw new ToolFailure('INVALID_SVG');
  });
  parser.on('xmldecl', (declaration) => {
    if (declaration.encoding && !/^utf-?8$/i.test(declaration.encoding))
      throw new ToolFailure('SVG_ENCODING_UNSUPPORTED');
  });
  parser.on('doctype', unsupported);
  parser.on('processinginstruction', (instruction) => {
    if (instruction.target === 'xml-stylesheet') unsupported();
  });
  parser.on('opentag', (tag) => {
    if (root) {
      root = false;
      if (tag.local !== 'svg' || tag.uri !== 'http://www.w3.org/2000/svg')
        throw new ToolFailure('INVALID_SVG');
      if (paper) {
        const millimeters = (name: string) => {
          const size =
            /^\s*(\+?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(mm|cm|in|pt|pc|q)\s*$/i.exec(
              tag.attributes[name]?.value ?? '',
            );
          if (!size) throw new ToolFailure('SVG_PHYSICAL_SIZE_REQUIRED');
          const units: Record<string, number> = {
            mm: 1,
            cm: 10,
            in: 25.4,
            pt: 25.4 / 72,
            pc: 25.4 / 6,
            q: 0.25,
          };
          const value = Number(size[1]) * units[size[2].toLowerCase()];
          if (!Number.isFinite(value) || value <= 0)
            throw new ToolFailure('SVG_PHYSICAL_SIZE_REQUIRED');
          return value;
        };
        // 比较物理尺寸并容许单位换算的常见小数取整；0.01 毫米仅用于纸张匹配。
        if (
          Math.abs(millimeters('width') - paper.widthMm) > 0.01 ||
          Math.abs(millimeters('height') - paper.heightMm) > 0.01
        )
          throw new ToolFailure('PAGE_SIZE_MISMATCH');
      }
    }
    for (const attribute of Object.values(tag.attributes)) {
      if (
        attribute.local === 'base' &&
        attribute.uri === 'http://www.w3.org/XML/1998/namespace' &&
        attribute.value
      )
        unsupported();
      if (attribute.local === 'href') reference(attribute.value);
      checkCss(attribute.value);
    }
    if (tag.local === 'style') styleDepth++;
  });
  parser.on('closetag', (tag) => {
    if (tag.local === 'style') {
      checkCss(style);
      if (--styleDepth === 0) style = '';
    }
  });
  parser.on('text', (value) => {
    if (styleDepth > 0) style += value;
  });
  parser.on('cdata', (value) => {
    if (styleDepth > 0) style += value;
  });
  parser.write(source).close();
}
