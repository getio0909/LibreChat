import { z } from 'zod';
import { ToolFailure } from './config.ts';
import { selfContainedSvg } from './svg.ts';

const PAPER_PRESETS: Record<string, { widthMm: number; heightMm: number }> = {
  A4: { widthMm: 297, heightMm: 210 },
  A3: { widthMm: 420, heightMm: 297 },
  A2: { widthMm: 594, heightMm: 420 },
  A1: { widthMm: 841, heightMm: 594 },
  A0: { widthMm: 1189, heightMm: 841 },
};

const rectStyleSchema = z
  .enum([
    'solid',
    'medium',
    'thin',
    'hidden',
    'center',
    'hatch45',
    'hatch135',
    'fill_light',
    'fill_dark',
    'opening',
  ])
  .default('solid');

const lineStyleSchema = z
  .enum(['solid', 'medium', 'thin', 'hidden', 'center', 'pitch'])
  .default('solid');

export const drawingSheetInputSchema = {
  svg_filename: z.string().regex(/\.svg$/i),
  pdf_filename: z
    .string()
    .regex(/\.pdf$/i)
    .optional(),
  paper: z
    .object({
      preset: z.enum(['A4', 'A3', 'A2', 'A1', 'A0']).optional(),
      orientation: z.enum(['landscape', 'portrait']).default('landscape'),
      width_mm: z.number().min(50).max(1200).optional(),
      height_mm: z.number().min(50).max(1200).optional(),
    })
    .optional(),
  title_block: z.object({
    project_name: z.string().min(1),
    sheet_title: z.string().min(1),
    subtitle: z.string().optional(),
    drawing_no: z.string().min(1),
    revision: z.string().default('A'),
    scale: z.string().default('1:15'),
    unit: z.string().default('mm'),
    projection: z.string().default('第三角法'),
    sheet_index: z.number().int().min(1).default(1),
    sheet_total: z.number().int().min(1).default(4),
    material_summary: z.string().optional(),
    status: z.string().default('生产施工图'),
    date: z.string().optional(),
  }),
  views: z
    .array(
      z.object({
        id: z.string().min(1),
        title: z.string().min(1),
        subtitle: z.string().optional(),
        scale: z.union([z.number().positive(), z.string().min(1)]),
        origin_mm: z.tuple([z.number(), z.number()]),
        width_model_mm: z.number().positive().optional(),
        height_model_mm: z.number().positive().optional(),
        rects: z
          .array(
            z.object({
              x: z.number(),
              y: z.number(),
              w: z.number().positive(),
              h: z.number().positive(),
              style: rectStyleSchema.optional(),
              rx: z.number().min(0).optional(),
              label: z.string().optional(),
            }),
          )
          .optional(),
        lines: z
          .array(
            z.object({
              x1: z.number(),
              y1: z.number(),
              x2: z.number(),
              y2: z.number(),
              style: lineStyleSchema.optional(),
            }),
          )
          .optional(),
        polylines: z
          .array(
            z.object({
              points: z.array(z.tuple([z.number(), z.number()])).min(2),
              closed: z.boolean().optional(),
              style: rectStyleSchema.optional(),
            }),
          )
          .optional(),
        circles: z
          .array(
            z.object({
              cx: z.number(),
              cy: z.number(),
              r: z.number().positive(),
              style: lineStyleSchema.optional(),
              crosshair: z.boolean().optional(),
              label: z.string().optional(),
            }),
          )
          .optional(),
        ellipses: z
          .array(
            z.object({
              cx: z.number(),
              cy: z.number(),
              rx: z.number().positive(),
              ry: z.number().positive(),
              style: lineStyleSchema.optional(),
              crosshair: z.boolean().optional(),
              label: z.string().optional(),
            }),
          )
          .optional(),
        arcs: z
          .array(
            z.object({
              cx: z.number(),
              cy: z.number(),
              r: z.number().positive(),
              start_deg: z.number(),
              end_deg: z.number(),
              style: lineStyleSchema.optional(),
            }),
          )
          .optional(),
        paths: z
          .array(
            z.object({
              d: z.string().min(1),
              style: rectStyleSchema.optional(),
            }),
          )
          .optional(),
        fretwork: z
          .array(
            z.object({
              x: z.number(),
              y: z.number(),
              w: z.number().positive(),
              h: z.number().positive(),
              cells: z.number().int().min(1).max(64).optional(),
              motif: z.enum(['meander', 'lattice', 'relief_frame']).default('meander'),
            }),
          )
          .optional(),
        part_tags: z
          .array(
            z.object({
              x: z.number(),
              y: z.number(),
              tag: z.string().min(1),
              label: z.string().optional(),
              dx_mm: z.number().optional(),
              dy_mm: z.number().optional(),
            }),
          )
          .optional(),
        texts: z
          .array(
            z.object({
              x: z.number(),
              y: z.number(),
              text: z.string().min(1),
              size_mm: z.number().min(1.5).max(10).optional(),
              align: z.enum(['start', 'middle', 'end']).optional(),
              bold: z.boolean().optional(),
              rotate: z.number().optional(),
              box: z.boolean().optional(),
            }),
          )
          .optional(),
        linear_dims: z
          .array(
            z.object({
              orientation: z.enum(['horizontal', 'vertical']),
              start: z.number(),
              end: z.number(),
              anchor: z.number(),
              offset_mm: z.number(),
              value: z.number().positive().optional(),
              label: z.string().optional(),
              prefix: z.string().optional(),
              suffix: z.string().optional(),
            }),
          )
          .optional(),
        radial_dims: z
          .array(
            z.object({
              cx: z.number(),
              cy: z.number(),
              r: z.number().positive(),
              angle_deg: z.number().default(45),
              mode: z.enum(['radius', 'diameter']).default('radius'),
              label: z.string().optional(),
            }),
          )
          .optional(),
        chain_dims: z
          .array(
            z.object({
              orientation: z.enum(['horizontal', 'vertical']),
              start: z.number(),
              anchor: z.number(),
              offset_mm: z.number(),
              segments: z.array(z.number().positive()).min(2),
              labels: z.array(z.string()).optional(),
              expected_total: z.number().positive().optional(),
              show_total: z.boolean().optional(),
              total_offset_mm: z.number().optional(),
            }),
          )
          .optional(),
        elevation_ladders: z
          .array(
            z.object({
              x_model: z.number(),
              datum_y_model: z.number(),
              direction: z.enum(['up', 'down']).default('up'),
              offset_mm: z.number(),
              levels: z
                .array(
                  z.object({
                    elevation: z.number(),
                    label: z.string().optional(),
                  }),
                )
                .min(1),
            }),
          )
          .optional(),
        section_marks: z
          .array(
            z.object({
              orientation: z.enum(['horizontal', 'vertical']),
              position_model: z.number(),
              start_model: z.number(),
              end_model: z.number(),
              label: z.string().min(1),
              direction: z.enum(['left', 'right', 'up', 'down']).optional(),
            }),
          )
          .optional(),
      }),
    )
    .default([]),
  details: z
    .array(
      z.object({
        id: z.string().min(1),
        type: z.enum([
          'dowel_cam_joint',
          'back_rabbet_joint',
          'recessed_handle_section',
          'fretwork_panel_joint',
          'mounting_plate_detail',
          'hinge_cup_detail',
          'custom_profile_detail',
        ]),
        title: z.string().min(1),
        scale_label: z.string().default('1:1'),
        x_mm: z.number(),
        y_mm: z.number(),
        w_mm: z.number().min(30),
        h_mm: z.number().min(25),
        params: z.record(z.string(), z.union([z.number(), z.string()])).optional(),
        polylines: z
          .array(
            z.object({
              points: z.array(z.tuple([z.number(), z.number()])).min(2),
              closed: z.boolean().optional(),
              style: rectStyleSchema.optional(),
            }),
          )
          .optional(),
        paths: z
          .array(
            z.object({
              d: z.string().min(1),
              style: rectStyleSchema.optional(),
            }),
          )
          .optional(),
        notes: z.array(z.string()).optional(),
      }),
    )
    .default([]),
  tables: z
    .array(
      z.object({
        title: z.string().min(1),
        subtitle: z.string().optional(),
        x_mm: z.number(),
        y_mm: z.number(),
        row_height_mm: z.number().min(3.8).max(12).default(5.2),
        columns: z
          .array(
            z.object({
              header: z.string().min(1),
              width_mm: z.number().min(6),
              align: z.enum(['left', 'center', 'right']).default('left'),
            }),
          )
          .min(1),
        rows: z.array(z.array(z.string())).min(1),
        footer_note: z.string().optional(),
      }),
    )
    .default([]),
  note_boxes: z
    .array(
      z.object({
        title: z.string().min(1),
        x_mm: z.number(),
        y_mm: z.number(),
        w_mm: z.number().min(30),
        h_mm: z.number().min(14).optional(),
        items: z.array(z.string().min(1)).min(1),
        style: z.enum(['standard', 'highlight']).default('standard'),
      }),
    )
    .default([]),
};

export const drawingSheetSchema = z.object(drawingSheetInputSchema);
export type DrawingSheetInput = z.infer<typeof drawingSheetSchema>;

export interface CompiledSheetResult {
  svgBytes: Buffer;
  paper: { widthMm: number; heightMm: number };
  verification: {
    drawing_no: string;
    sheet_title: string;
    page_size_mm: [number, number];
    view_count: number;
    detail_count: number;
    table_count: number;
    note_box_count: number;
    verified_dimension_chains: {
      view_id: string;
      orientation: 'horizontal' | 'vertical';
      segments: number[];
      sum_mm: number;
      expected_total_mm?: number;
    }[];
    verified_linear_dims: number;
    bom_row_count: number;
  };
}

function esc(raw: string | number): string {
  return String(raw)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

function fmt(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(2).replace(/\.?0+$/, '');
}

function parseScale(scale: number | string): { ratio: number; label: string } {
  if (typeof scale === 'number') {
    if (!Number.isFinite(scale) || scale <= 0) throw new ToolFailure('INVALID_DRAWING_SCALE');
    if (scale > 1) {
      return { ratio: 1 / scale, label: `1:${fmt(scale)}` };
    }
    if (scale === 1) {
      return { ratio: 1, label: '1:1' };
    }
    return { ratio: scale, label: `1:${fmt(1 / scale)}` };
  }
  const trimmed = scale.trim();
  const colon = /^(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)$/.exec(trimmed);
  if (colon) {
    const num = Number(colon[1]);
    const den = Number(colon[2]);
    if (num <= 0 || den <= 0) throw new ToolFailure('INVALID_DRAWING_SCALE');
    return { ratio: num / den, label: `${fmt(num)}:${fmt(den)}` };
  }
  const numeric = Number(trimmed);
  if (Number.isFinite(numeric) && numeric > 0) {
    return parseScale(numeric);
  }
  throw new ToolFailure('INVALID_DRAWING_SCALE', `Unsupported scale format: ${scale}`);
}

function resolvePaper(paper?: DrawingSheetInput['paper']): { widthMm: number; heightMm: number } {
  if (paper?.width_mm != null || paper?.height_mm != null) {
    if (paper.width_mm == null || paper.height_mm == null) {
      throw new ToolFailure('PAGE_SIZE_REQUIRED');
    }
    return { widthMm: paper.width_mm, heightMm: paper.height_mm };
  }
  const preset = PAPER_PRESETS[paper?.preset ?? 'A3'];
  if (paper?.orientation === 'portrait') {
    return {
      widthMm: Math.min(preset.widthMm, preset.heightMm),
      heightMm: Math.max(preset.widthMm, preset.heightMm),
    };
  }
  return {
    widthMm: Math.max(preset.widthMm, preset.heightMm),
    heightMm: Math.min(preset.widthMm, preset.heightMm),
  };
}

function rectAttrs(style: z.infer<typeof rectStyleSchema>): string {
  switch (style) {
    case 'solid':
      return 'fill="none" stroke="#111111" stroke-width="0.35"';
    case 'medium':
      return 'fill="none" stroke="#111111" stroke-width="0.25"';
    case 'thin':
      return 'fill="none" stroke="#222222" stroke-width="0.18"';
    case 'hidden':
      return 'fill="none" stroke="#444444" stroke-width="0.18" stroke-dasharray="1.8,1.0"';
    case 'center':
      return 'fill="none" stroke="#555555" stroke-width="0.13" stroke-dasharray="4.0,1.2,0.8,1.2"';
    case 'hatch45':
      return 'fill="url(#hatch45)" stroke="#111111" stroke-width="0.30"';
    case 'hatch135':
      return 'fill="url(#hatch135)" stroke="#111111" stroke-width="0.30"';
    case 'fill_light':
      return 'fill="#f5f5f5" stroke="#111111" stroke-width="0.25"';
    case 'fill_dark':
      return 'fill="#e2e2e2" stroke="#111111" stroke-width="0.25"';
    case 'opening':
      return 'fill="#fafafa" stroke="#222222" stroke-width="0.22"';
  }
}

function lineAttrs(style: z.infer<typeof lineStyleSchema>): string {
  switch (style) {
    case 'solid':
      return 'stroke="#111111" stroke-width="0.35"';
    case 'medium':
      return 'stroke="#111111" stroke-width="0.25"';
    case 'thin':
      return 'stroke="#222222" stroke-width="0.18"';
    case 'hidden':
      return 'stroke="#444444" stroke-width="0.18" stroke-dasharray="1.8,1.0"';
    case 'center':
      return 'stroke="#555555" stroke-width="0.13" stroke-dasharray="4.0,1.2,0.8,1.2"';
    case 'pitch':
      return 'stroke="#444444" stroke-width="0.14" stroke-dasharray="1.0,1.0"';
  }
}

function drawArrow(
  x: number,
  y: number,
  dir: 'left' | 'right' | 'up' | 'down',
  len = 2.0,
  halfW = 0.55,
): string {
  if (dir === 'left') {
    return `<polygon points="${fmt(x)},${fmt(y)} ${fmt(x + len)},${fmt(y - halfW)} ${fmt(x + len)},${fmt(y + halfW)}" fill="#111111"/>`;
  }
  if (dir === 'right') {
    return `<polygon points="${fmt(x)},${fmt(y)} ${fmt(x - len)},${fmt(y - halfW)} ${fmt(x - len)},${fmt(y + halfW)}" fill="#111111"/>`;
  }
  if (dir === 'up') {
    return `<polygon points="${fmt(x)},${fmt(y)} ${fmt(x - halfW)},${fmt(y + len)} ${fmt(x + halfW)},${fmt(y + len)}" fill="#111111"/>`;
  }
  return `<polygon points="${fmt(x)},${fmt(y)} ${fmt(x - halfW)},${fmt(y - len)} ${fmt(x + halfW)},${fmt(y - len)}" fill="#111111"/>`;
}

function drawHorizontalDim(
  x1: number,
  x2: number,
  anchorY: number,
  dimY: number,
  label: string,
  staggerIdx = 0,
): string {
  const left = Math.min(x1, x2);
  const right = Math.max(x1, x2);
  const span = right - left;
  const sign = dimY >= anchorY ? 1 : -1;
  const extStart = anchorY + sign * 0.8;
  const extEnd = dimY + sign * 1.4;
  const midX = (left + right) / 2;
  const narrow = span < 6.0;
  const textY = narrow ? dimY + (staggerIdx % 2 === 0 ? -1.4 : 2.8) : dimY - 0.9;
  const parts = [
    `<line x1="${fmt(left)}" y1="${fmt(extStart)}" x2="${fmt(left)}" y2="${fmt(extEnd)}" stroke="#222222" stroke-width="0.15"/>`,
    `<line x1="${fmt(right)}" y1="${fmt(extStart)}" x2="${fmt(right)}" y2="${fmt(extEnd)}" stroke="#222222" stroke-width="0.15"/>`,
    `<line x1="${fmt(left)}" y1="${fmt(dimY)}" x2="${fmt(right)}" y2="${fmt(dimY)}" stroke="#111111" stroke-width="0.18"/>`,
  ];
  if (span >= 4.2) {
    parts.push(drawArrow(left, dimY, 'left', 1.8, 0.5));
    parts.push(drawArrow(right, dimY, 'right', 1.8, 0.5));
  } else {
    // 窄间距段采用建筑/家具制图 45° 斜Tick标记，防止箭头互相重叠。
    parts.push(
      `<line x1="${fmt(left - 0.7)}" y1="${fmt(dimY + 0.7)}" x2="${fmt(left + 0.7)}" y2="${fmt(dimY - 0.7)}" stroke="#111111" stroke-width="0.24"/>`,
      `<line x1="${fmt(right - 0.7)}" y1="${fmt(dimY + 0.7)}" x2="${fmt(right + 0.7)}" y2="${fmt(dimY - 0.7)}" stroke="#111111" stroke-width="0.24"/>`,
    );
  }
  parts.push(
    `<text x="${fmt(midX)}" y="${fmt(textY)}" font-size="${narrow ? '2.0' : '2.35'}" text-anchor="middle" fill="#111111">${esc(label)}</text>`,
  );
  return parts.join('\n');
}

function drawVerticalDim(
  y1: number,
  y2: number,
  anchorX: number,
  dimX: number,
  label: string,
  staggerIdx = 0,
): string {
  const top = Math.min(y1, y2);
  const bottom = Math.max(y1, y2);
  const span = bottom - top;
  const sign = dimX >= anchorX ? 1 : -1;
  const extStart = anchorX + sign * 0.8;
  const extEnd = dimX + sign * 1.4;
  const midY = (top + bottom) / 2;
  const narrow = span < 6.0;
  const textX = narrow ? dimX + (staggerIdx % 2 === 0 ? -1.5 : 2.6) : dimX - 1.0;
  const parts = [
    `<line x1="${fmt(extStart)}" y1="${fmt(top)}" x2="${fmt(extEnd)}" y2="${fmt(top)}" stroke="#222222" stroke-width="0.15"/>`,
    `<line x1="${fmt(extStart)}" y1="${fmt(bottom)}" x2="${fmt(extEnd)}" y2="${fmt(bottom)}" stroke="#222222" stroke-width="0.15"/>`,
    `<line x1="${fmt(dimX)}" y1="${fmt(top)}" x2="${fmt(dimX)}" y2="${fmt(bottom)}" stroke="#111111" stroke-width="0.18"/>`,
  ];
  if (span >= 4.2) {
    parts.push(drawArrow(dimX, top, 'up', 1.8, 0.5));
    parts.push(drawArrow(dimX, bottom, 'down', 1.8, 0.5));
  } else {
    parts.push(
      `<line x1="${fmt(dimX - 0.7)}" y1="${fmt(top + 0.7)}" x2="${fmt(dimX + 0.7)}" y2="${fmt(top - 0.7)}" stroke="#111111" stroke-width="0.24"/>`,
      `<line x1="${fmt(dimX - 0.7)}" y1="${fmt(bottom + 0.7)}" x2="${fmt(dimX + 0.7)}" y2="${fmt(bottom - 0.7)}" stroke="#111111" stroke-width="0.24"/>`,
    );
  }
  parts.push(
    `<text x="${fmt(textX)}" y="${fmt(midY)}" font-size="${narrow ? '2.0' : '2.35'}" text-anchor="middle" fill="#111111" transform="rotate(-90 ${fmt(textX)} ${fmt(midY)})">${esc(label)}</text>`,
  );
  return parts.join('\n');
}

function drawFretworkBand(
  x: number,
  y: number,
  w: number,
  h: number,
  cells: number,
  motif: 'meander' | 'lattice' | 'relief_frame',
): string {
  const out: string[] = [
    `<rect x="${fmt(x)}" y="${fmt(y)}" width="${fmt(w)}" height="${fmt(h)}" fill="none" stroke="#111111" stroke-width="0.24"/>`,
  ];
  const cellW = w / cells;
  for (let i = 0; i < cells; i++) {
    const cx = x + i * cellW;
    if (i > 0) {
      out.push(
        `<line x1="${fmt(cx)}" y1="${fmt(y)}" x2="${fmt(cx)}" y2="${fmt(y + h)}" stroke="#222222" stroke-width="0.15"/>`,
      );
    }
    const padX = cellW * 0.14;
    const padY = h * 0.16;
    const ix = cx + padX;
    const iy = y + padY;
    const iw = Math.max(0.4, cellW - 2 * padX);
    const ih = Math.max(0.4, h - 2 * padY);
    if (motif === 'meander') {
      // 中式回纹几何螺旋单元
      const p1x = ix;
      const p1y = iy + ih;
      const p2x = ix;
      const p2y = iy;
      const p3x = ix + iw;
      const p3y = iy;
      const p4x = ix + iw;
      const p4y = iy + ih * 0.72;
      const p5x = ix + iw * 0.32;
      const p5y = iy + ih * 0.72;
      const p6x = ix + iw * 0.32;
      const p6y = iy + ih * 0.32;
      const p7x = ix + iw * 0.68;
      const p7y = iy + ih * 0.32;
      out.push(
        `<polyline points="${fmt(p1x)},${fmt(p1y)} ${fmt(p2x)},${fmt(p2y)} ${fmt(p3x)},${fmt(p3y)} ${fmt(p4x)},${fmt(p4y)} ${fmt(p5x)},${fmt(p5y)} ${fmt(p6x)},${fmt(p6y)} ${fmt(p7x)},${fmt(p7y)}" fill="none" stroke="#222222" stroke-width="0.14"/>`,
      );
    } else if (motif === 'lattice') {
      out.push(
        `<line x1="${fmt(ix)}" y1="${fmt(iy + ih / 2)}" x2="${fmt(ix + iw)}" y2="${fmt(iy + ih / 2)}" stroke="#222222" stroke-width="0.14"/>`,
        `<line x1="${fmt(ix + iw / 2)}" y1="${fmt(iy)}" x2="${fmt(ix + iw / 2)}" y2="${fmt(iy + ih)}" stroke="#222222" stroke-width="0.14"/>`,
      );
    } else {
      out.push(
        `<rect x="${fmt(ix)}" y="${fmt(iy)}" width="${fmt(iw)}" height="${fmt(ih)}" fill="none" stroke="#222222" stroke-width="0.14"/>`,
      );
    }
  }
  return out.join('\n');
}

function renderDetailBlock(detail: DrawingSheetInput['details'][number]): string {
  const { x_mm: x, y_mm: y, w_mm: w, h_mm: h, title, scale_label, type, params = {} } = detail;
  const out: string[] = [
    `<g id="detail-${esc(detail.id)}">`,
    `<rect x="${fmt(x)}" y="${fmt(y)}" width="${fmt(w)}" height="${fmt(h)}" fill="none" stroke="#111111" stroke-width="0.28"/>`,
    `<rect x="${fmt(x)}" y="${fmt(y)}" width="${fmt(w)}" height="6.2" fill="#f3f3f3" stroke="#111111" stroke-width="0.22"/>`,
    `<text x="${fmt(x + 2.5)}" y="${fmt(y + 4.3)}" font-size="2.7" font-weight="bold" fill="#111111">${esc(title)}</text>`,
    `<text x="${fmt(x + w - 2.5)}" y="${fmt(y + 4.3)}" font-size="2.3" text-anchor="end" fill="#222222">比例 ${esc(scale_label)}</text>`,
  ];
  const bx = x + 6;
  const by = y + 10;
  const bw = w - 12;
  const bh = h - 14;

  if (type === 'dowel_cam_joint') {
    const sideT = Number(params.side_thickness_mm ?? 18);
    const shelfT = Number(params.shelf_thickness_mm ?? 22);
    const dowelD = Number(params.dowel_diameter_mm ?? 8);
    const dowelL = Number(params.dowel_length_mm ?? 30);
    const sx = bx + Math.min(14, bw * 0.18);
    const sy = by + 2;
    const sideW = Math.min(20, Math.max(12, sideT));
    const sideH = Math.min(bh - 8, 42);
    const shelfY = sy + sideH * 0.36;
    const shelfH = Math.min(22, Math.max(12, shelfT));
    const shelfW = Math.min(bw - (sx - bx) - sideW - 8, 52);
    out.push(
      `<rect x="${fmt(sx)}" y="${fmt(sy)}" width="${fmt(sideW)}" height="${fmt(sideH)}" ${rectAttrs('hatch45')}/>`,
      `<rect x="${fmt(sx + sideW)}" y="${fmt(shelfY)}" width="${fmt(shelfW)}" height="${fmt(shelfH)}" ${rectAttrs('hatch135')}/>`,
      // Ø8×30 圆棒榫
      `<rect x="${fmt(sx + sideW - 10)}" y="${fmt(shelfY + shelfH * 0.25)}" width="24" height="5.5" fill="#e6e6e6" stroke="#111111" stroke-width="0.25"/>`,
      `<line x1="${fmt(sx + sideW - 12)}" y1="${fmt(shelfY + shelfH * 0.25 + 2.75)}" x2="${fmt(sx + sideW + 16)}" y2="${fmt(shelfY + shelfH * 0.25 + 2.75)}" ${lineAttrs('center')}/>`,
      // 偏心连接杆与沉头螺钉
      `<rect x="${fmt(sx + 3)}" y="${fmt(shelfY + shelfH * 0.68)}" width="${fmt(sideW + 20)}" height="2.8" fill="#444444" stroke="#111111" stroke-width="0.20"/>`,
      `<circle cx="${fmt(sx + sideW + 20)}" cy="${fmt(shelfY + shelfH * 0.68 + 1.4)}" r="3.8" fill="#ffffff" stroke="#111111" stroke-width="0.25"/>`,
      drawHorizontalDim(sx, sx + sideW, sy, sy - 2.6, `侧板 ${fmt(sideT)}`),
      drawVerticalDim(
        shelfY,
        shelfY + shelfH,
        sx + sideW + shelfW,
        sx + sideW + shelfW + 4.5,
        `层板 ${fmt(shelfT)}`,
      ),
      `<text x="${fmt(sx + sideW + 4)}" y="${fmt(shelfY - 2.2)}" font-size="2.2" fill="#111111">Ø${fmt(dowelD)}×${fmt(dowelL)} 木榫 + 三合一偏心件 (双连接防转)</text>`,
      `<text x="${fmt(sx)}" y="${fmt(sy + sideH + 4.2)}" font-size="2.1" fill="#222222">注：接触面涂 D3 木工白胶，止口预留 0.5mm 胶缝间隙</text>`,
    );
  } else if (type === 'back_rabbet_joint') {
    const sideT = Number(params.side_thickness_mm ?? 18);
    const backT = Number(params.back_thickness_mm ?? 9);
    const rabbetW = Number(params.rabbet_width_mm ?? 8);
    const rabbetD = Number(params.rabbet_depth_mm ?? 9);
    const sx = bx + 10;
    const sy = by + 4;
    const panelW = Math.min(bw - 24, 54);
    const panelH = Math.min(18, Math.max(12, sideT));
    out.push(
      // 带止口的侧板后缘截面
      `<polygon points="${fmt(sx)},${fmt(sy)} ${fmt(sx + panelW)},${fmt(sy)} ${fmt(sx + panelW)},${fmt(sy + panelH - rabbetW)} ${fmt(sx + panelW - rabbetD)},${fmt(sy + panelH - rabbetW)} ${fmt(sx + panelW - rabbetD)},${fmt(sy + panelH)} ${fmt(sx)},${fmt(sy + panelH)}" ${rectAttrs('hatch45')}/>`,
      // 9mm 背板
      `<rect x="${fmt(sx + panelW - rabbetD)}" y="${fmt(sy + panelH - rabbetW)}" width="${fmt(rabbetD)}" height="${fmt(Math.min(bh - 16, 30))}" ${rectAttrs('hatch135')}/>`,
      // 30×15 防潮加固背带
      `<rect x="${fmt(sx + panelW - rabbetD - 13)}" y="${fmt(sy + panelH + 1.5)}" width="13" height="16" ${rectAttrs('fill_light')}/>`,
      drawHorizontalDim(sx + panelW - rabbetD, sx + panelW, sy, sy - 2.5, `止口深 ${fmt(rabbetD)}`),
      drawVerticalDim(sy, sy + panelH, sx, sx - 4.0, `侧板 ${fmt(sideT)}`),
      `<text x="${fmt(sx + 2)}" y="${fmt(sy + panelH + 7)}" font-size="2.2" fill="#111111">背板 δ=${fmt(backT)} 嵌入 ${fmt(rabbetW)}×${fmt(rabbetD)} 止口槽</text>`,
      `<text x="${fmt(sx + 2)}" y="${fmt(sy + panelH + 11.5)}" font-size="2.1" fill="#222222">后背加 30×15 实木防潮背带，Ø3.5×25 螺钉 @200 固定</text>`,
    );
  } else if (type === 'recessed_handle_section') {
    const doorT = Number(params.door_thickness_mm ?? 22);
    const grooveW = Number(params.groove_height_mm ?? 35);
    const grooveD = Number(params.groove_depth_mm ?? 14);
    const sx = bx + 12;
    const sy = by + 4;
    const dh = Math.min(bh - 12, 38);
    const dw = Math.min(24, Math.max(14, doorT));
    const gy = sy + 6;
    const gh = Math.min(dh - 12, 18);
    const gd = Math.min(dw * 0.62, 12);
    out.push(
      `<polygon points="${fmt(sx)},${fmt(sy)} ${fmt(sx + dw)},${fmt(sy)} ${fmt(sx + dw)},${fmt(gy)} ${fmt(sx + dw - gd)},${fmt(gy)} ${fmt(sx + dw - gd)},${fmt(gy + gh)} ${fmt(sx + dw)},${fmt(gy + gh)} ${fmt(sx + dw)},${fmt(sy + dh)} ${fmt(sx)},${fmt(sy + dh)}" ${rectAttrs('hatch45')}/>`,
      drawHorizontalDim(sx, sx + dw, sy, sy - 2.5, `门厚 ${fmt(doorT)}`),
      drawHorizontalDim(sx + dw - gd, sx + dw, sy + dh, sy + dh + 3.5, `槽深 ${fmt(grooveD)}`),
      drawVerticalDim(gy, gy + gh, sx + dw, sx + dw + 5.0, `槽高 ${fmt(grooveW)}`),
      `<text x="${fmt(sx + dw + 10)}" y="${fmt(sy + 10)}" font-size="2.2" fill="#111111">内抠式暗拉手槽 1:1 截面</text>`,
      `<text x="${fmt(sx + dw + 10)}" y="${fmt(sy + 14.5)}" font-size="2.1" fill="#222222">外缘及指槽内口均倒圆 R2，手工砂光 320#</text>`,
      `<text x="${fmt(sx + dw + 10)}" y="${fmt(sy + 19.0)}" font-size="2.1" fill="#222222">双门对缝预留 2.5mm 均匀门缝</text>`,
    );
  } else if (type === 'fretwork_panel_joint') {
    const frameT = Number(params.frame_thickness_mm ?? 18);
    const reliefD = Number(params.relief_depth_mm ?? 4);
    const innerR = Number(params.cutter_radius_mm ?? 1.5);
    const sx = bx + 8;
    const sy = by + 4;
    const pw = Math.min(bw - 18, 58);
    const ph = Math.min(bh - 14, 24);
    out.push(
      `<rect x="${fmt(sx)}" y="${fmt(sy)}" width="${fmt(pw)}" height="${fmt(ph)}" ${rectAttrs('solid')}/>`,
      drawFretworkBand(sx + 3, sy + 3, pw - 6, ph - 6, 4, 'meander'),
      `<text x="${fmt(sx)}" y="${fmt(sy + ph + 4.5)}" font-size="2.2" fill="#111111">中式回纹/雕花构件：板厚 ${fmt(frameT)}mm，浮雕深 ${fmt(reliefD)}mm</text>`,
      `<text x="${fmt(sx)}" y="${fmt(sy + ph + 8.5)}" font-size="2.1" fill="#222222">CNC 精雕清角内圆角 R≥${fmt(innerR)}mm，框料格肩榫拼合，顺纹水平通长</text>`,
    );
  } else if (type === 'mounting_plate_detail') {
    const plateW = Number(params.plate_width_mm ?? 100);
    const plateH = Number(params.plate_depth_mm ?? 80);
    const holePitchX = Number(params.pitch_x_mm ?? 76);
    const holePitchY = Number(params.pitch_y_mm ?? 56);
    const holeD = Number(params.hole_diameter_mm ?? 6.5);
    const pw = Math.min(bw - 24, 46);
    const ph = Math.min(bh - 16, 32);
    const px = bx + 10;
    const py = by + 4;
    const insetX = pw * 0.14;
    const insetY = ph * 0.16;
    const holes: [number, number][] = [
      [px + insetX, py + insetY],
      [px + pw - insetX, py + insetY],
      [px + insetX, py + ph - insetY],
      [px + pw - insetX, py + ph - insetY],
    ];
    out.push(
      `<rect x="${fmt(px)}" y="${fmt(py)}" width="${fmt(pw)}" height="${fmt(ph)}" ${rectAttrs('fill_light')}/>`,
      `<line x1="${fmt(px - 2)}" y1="${fmt(py + ph / 2)}" x2="${fmt(px + pw + 2)}" y2="${fmt(py + ph / 2)}" ${lineAttrs('center')}/>`,
      `<line x1="${fmt(px + pw / 2)}" y1="${fmt(py - 2)}" x2="${fmt(px + pw / 2)}" y2="${fmt(py + ph + 2)}" ${lineAttrs('center')}/>`,
      `<circle cx="${fmt(px + pw / 2)}" cy="${fmt(py + ph / 2)}" r="3.4" ${lineAttrs('solid')} fill="#ffffff"/>`,
    );
    for (const [hx, hy] of holes) {
      out.push(
        `<circle cx="${fmt(hx)}" cy="${fmt(hy)}" r="1.6" fill="#ffffff" stroke="#111111" stroke-width="0.22"/>`,
        `<line x1="${fmt(hx - 2.4)}" y1="${fmt(hy)}" x2="${fmt(hx + 2.4)}" y2="${fmt(hy)}" ${lineAttrs('center')}/>`,
        `<line x1="${fmt(hx)}" y1="${fmt(hy - 2.4)}" x2="${fmt(hx)}" y2="${fmt(hy + 2.4)}" ${lineAttrs('center')}/>`,
      );
    }
    out.push(
      drawHorizontalDim(
        px,
        px + pw,
        py + ph,
        py + ph + 4.5,
        `${fmt(plateW)} (孔距 ${fmt(holePitchX)})`,
      ),
      drawVerticalDim(
        py,
        py + ph,
        px + pw,
        px + pw + 5.0,
        `${fmt(plateH)} (孔距 ${fmt(holePitchY)})`,
      ),
      `<text x="${fmt(px + pw + 10)}" y="${fmt(py + 8)}" font-size="2.2" fill="#111111">4-Ø${fmt(holeD)} 沉头安装孔</text>`,
      `<text x="${fmt(px + pw + 10)}" y="${fmt(py + 12.5)}" font-size="2.1" fill="#222222">中心 M10 焊母配可调地脚 (±10mm)</text>`,
      `<text x="${fmt(px + pw + 10)}" y="${fmt(py + 17.0)}" font-size="2.1" fill="#222222">Q235 钢板 δ=4mm 哑光黑烤漆</text>`,
    );
  } else if (type === 'hinge_cup_detail') {
    const cupD = Number(params.cup_diameter_mm ?? 35);
    const cupDepth = Number(params.cup_depth_mm ?? 12.5);
    const edgeK = Number(params.edge_offset_mm ?? 5);
    const pitch = Number(params.screw_pitch_mm ?? 48);
    const hx = bx + 14;
    const hy = by + 4;
    const doorH = Math.min(bh - 14, 32);
    const cupCy = hy + doorH / 2;
    const cupCx = hx + 14;
    out.push(
      `<rect x="${fmt(hx)}" y="${fmt(hy)}" width="36" height="${fmt(doorH)}" ${rectAttrs('solid')}/>`,
      `<circle cx="${fmt(cupCx)}" cy="${fmt(cupCy)}" r="7.5" fill="#f3f3f3" stroke="#111111" stroke-width="0.28"/>`,
      `<line x1="${fmt(cupCx - 9)}" y1="${fmt(cupCy)}" x2="${fmt(cupCx + 9)}" y2="${fmt(cupCy)}" ${lineAttrs('center')}/>`,
      `<line x1="${fmt(cupCx)}" y1="${fmt(cupCy - 11)}" x2="${fmt(cupCx)}" y2="${fmt(cupCy + 11)}" ${lineAttrs('center')}/>`,
      `<circle cx="${fmt(cupCx + 3.5)}" cy="${fmt(cupCy - 8.5)}" r="1.2" fill="#ffffff" stroke="#111111" stroke-width="0.20"/>`,
      `<circle cx="${fmt(cupCx + 3.5)}" cy="${fmt(cupCy + 8.5)}" r="1.2" fill="#ffffff" stroke="#111111" stroke-width="0.20"/>`,
      `<text x="${fmt(hx + 40)}" y="${fmt(hy + 8)}" font-size="2.2" fill="#111111">Ø${fmt(cupD)}×${fmt(cupDepth)} 铰链杯孔</text>`,
      `<text x="${fmt(hx + 40)}" y="${fmt(hy + 12.5)}" font-size="2.1" fill="#222222">边距 K=${fmt(edgeK)}mm，副孔距 ${fmt(pitch)}×6mm</text>`,
      `<text x="${fmt(hx + 40)}" y="${fmt(hy + 17.0)}" font-size="2.1" fill="#222222">配 110° 全盖阻尼液压铰链</text>`,
    );
  } else {
    // custom_profile_detail: 支持异形线脚、飞檐翘头、束腰莲座、特殊雕刻截面 1:1 / 1:2 大样
    for (const p of detail.polylines ?? []) {
      const pts = p.points.map(([px, py]) => `${fmt(bx + px)},${fmt(by + py)}`).join(' ');
      const tag = p.closed ? 'polygon' : 'polyline';
      out.push(`<${tag} points="${pts}" ${rectAttrs(p.style ?? 'hatch45')}/>`);
    }
    for (const pathItem of detail.paths ?? []) {
      out.push(
        `<g transform="translate(${fmt(bx)}, ${fmt(by)})"><path d="${esc(pathItem.d)}" ${rectAttrs(pathItem.style ?? 'solid')}/></g>`,
      );
    }
    const detailNotes = detail.notes ?? [
      String(params.note_1 ?? '异形曲面/线脚成型大样，模板及 CNC 刀路按 3D STEP 实体校核'),
      String(params.note_2 ?? '内转角清角半径 R≥1.5mm，外露棱边倒圆 R1.5 并 320# 精砂'),
    ];
    for (let i = 0; i < detailNotes.length; i++) {
      out.push(
        `<text x="${fmt(bx)}" y="${fmt(y + h - 2.5 - (detailNotes.length - 1 - i) * 4.2)}" font-size="2.1" fill="#111111">${esc(detailNotes[i])}</text>`,
      );
    }
  }

  out.push('</g>');
  return out.join('\n');
}

function renderTable(table: DrawingSheetInput['tables'][number]): string {
  const {
    x_mm: x,
    y_mm: y,
    title,
    subtitle,
    columns,
    rows,
    row_height_mm: rowH,
    footer_note,
  } = table;
  const totalW = columns.reduce((sum, col) => sum + col.width_mm, 0);
  const headerBannerH = 6.5;
  const colHeaderH = 5.6;
  const bodyH = rows.length * rowH;
  const footerH = footer_note ? 5.2 : 0;
  const totalH = headerBannerH + colHeaderH + bodyH + footerH;
  const out: string[] = [
    `<g class="schedule-table">`,
    `<rect x="${fmt(x)}" y="${fmt(y)}" width="${fmt(totalW)}" height="${fmt(totalH)}" fill="#ffffff" stroke="#111111" stroke-width="0.30"/>`,
    `<rect x="${fmt(x)}" y="${fmt(y)}" width="${fmt(totalW)}" height="${fmt(headerBannerH)}" fill="#eaeaea" stroke="#111111" stroke-width="0.25"/>`,
    `<text x="${fmt(x + 2.5)}" y="${fmt(y + 4.5)}" font-size="2.9" font-weight="bold" fill="#111111">${esc(title)}</text>`,
  ];
  if (subtitle) {
    out.push(
      `<text x="${fmt(x + totalW - 2.5)}" y="${fmt(y + 4.4)}" font-size="2.2" text-anchor="end" fill="#333333">${esc(subtitle)}</text>`,
    );
  }
  const colY = y + headerBannerH;
  out.push(
    `<rect x="${fmt(x)}" y="${fmt(colY)}" width="${fmt(totalW)}" height="${fmt(colHeaderH)}" fill="#f5f5f5" stroke="#111111" stroke-width="0.22"/>`,
  );
  let colX = x;
  for (let c = 0; c < columns.length; c++) {
    const col = columns[c];
    if (c > 0) {
      out.push(
        `<line x1="${fmt(colX)}" y1="${fmt(colY)}" x2="${fmt(colX)}" y2="${fmt(colY + colHeaderH + bodyH)}" stroke="#222222" stroke-width="0.18"/>`,
      );
    }
    const anchor = col.align === 'center' ? 'middle' : col.align === 'right' ? 'end' : 'start';
    const textX =
      col.align === 'center'
        ? colX + col.width_mm / 2
        : col.align === 'right'
          ? colX + col.width_mm - 1.8
          : colX + 1.8;
    out.push(
      `<text x="${fmt(textX)}" y="${fmt(colY + 3.9)}" font-size="2.35" font-weight="bold" text-anchor="${anchor}" fill="#111111">${esc(col.header)}</text>`,
    );
    colX += col.width_mm;
  }
  for (let r = 0; r < rows.length; r++) {
    const ry = colY + colHeaderH + r * rowH;
    out.push(
      `<line x1="${fmt(x)}" y1="${fmt(ry)}" x2="${fmt(x + totalW)}" y2="${fmt(ry)}" stroke="#222222" stroke-width="0.16"/>`,
    );
    let cellX = x;
    const row = rows[r];
    for (let c = 0; c < columns.length; c++) {
      const col = columns[c];
      const cell = row[c] ?? '';
      const anchor = col.align === 'center' ? 'middle' : col.align === 'right' ? 'end' : 'start';
      const textX =
        col.align === 'center'
          ? cellX + col.width_mm / 2
          : col.align === 'right'
            ? cellX + col.width_mm - 1.8
            : cellX + 1.8;
      out.push(
        `<text x="${fmt(textX)}" y="${fmt(ry + rowH * 0.68)}" font-size="2.2" text-anchor="${anchor}" fill="#111111">${esc(cell)}</text>`,
      );
      cellX += col.width_mm;
    }
  }
  if (footer_note) {
    const fy = colY + colHeaderH + bodyH;
    out.push(
      `<line x1="${fmt(x)}" y1="${fmt(fy)}" x2="${fmt(x + totalW)}" y2="${fmt(fy)}" stroke="#111111" stroke-width="0.22"/>`,
      `<text x="${fmt(x + 2.2)}" y="${fmt(fy + 3.6)}" font-size="2.1" fill="#222222">${esc(footer_note)}</text>`,
    );
  }
  out.push('</g>');
  return out.join('\n');
}

function renderNoteBox(box: DrawingSheetInput['note_boxes'][number]): string {
  const { x_mm: x, y_mm: y, w_mm: w, title, items, style } = box;
  const lineH = 4.4;
  const h = box.h_mm ?? Math.max(16, 7.5 + items.length * lineH);
  const headerFill = style === 'highlight' ? '#e8eef5' : '#f2f2f2';
  const out: string[] = [
    `<g class="note-box">`,
    `<rect x="${fmt(x)}" y="${fmt(y)}" width="${fmt(w)}" height="${fmt(h)}" fill="#ffffff" stroke="#111111" stroke-width="0.25"/>`,
    `<rect x="${fmt(x)}" y="${fmt(y)}" width="${fmt(w)}" height="5.8" fill="${headerFill}" stroke="#111111" stroke-width="0.20"/>`,
    `<text x="${fmt(x + 2.5)}" y="${fmt(y + 4.0)}" font-size="2.6" font-weight="bold" fill="#111111">${esc(title)}</text>`,
  ];
  for (let i = 0; i < items.length; i++) {
    const ty = y + 9.6 + i * lineH;
    if (ty <= y + h - 1.2) {
      out.push(
        `<text x="${fmt(x + 2.5)}" y="${fmt(ty)}" font-size="2.15" fill="#111111">${esc(items[i])}</text>`,
      );
    }
  }
  out.push('</g>');
  return out.join('\n');
}

function renderTitleBlock(
  paper: { widthMm: number; heightMm: number },
  tb: DrawingSheetInput['title_block'],
): string {
  const w = 152;
  const h = 24;
  const x = paper.widthMm - 10 - w;
  const y = paper.heightMm - 10 - h;
  const dateStr = tb.date ?? new Date().toISOString().slice(0, 10);
  const matStr = tb.material_summary ?? '实木主框 + 多层实木贴皮';
  return [
    `<g id="title-block">`,
    `<rect x="${fmt(x)}" y="${fmt(y)}" width="${fmt(w)}" height="${fmt(h)}" fill="#ffffff" stroke="#111111" stroke-width="0.35"/>`,
    `<line x1="${fmt(x)}" y1="${fmt(y + 8)}" x2="${fmt(x + w)}" y2="${fmt(y + 8)}" stroke="#111111" stroke-width="0.22"/>`,
    `<line x1="${fmt(x)}" y1="${fmt(y + 16)}" x2="${fmt(x + w)}" y2="${fmt(y + 16)}" stroke="#111111" stroke-width="0.22"/>`,
    `<line x1="${fmt(x + 88)}" y1="${fmt(y)}" x2="${fmt(x + 88)}" y2="${fmt(y + h)}" stroke="#111111" stroke-width="0.22"/>`,
    `<line x1="${fmt(x + 120)}" y1="${fmt(y + 8)}" x2="${fmt(x + 120)}" y2="${fmt(y + h)}" stroke="#111111" stroke-width="0.22"/>`,
    `<text x="${fmt(x + 3)}" y="${fmt(y + 5.3)}" font-size="2.9" font-weight="bold" fill="#111111">项目：${esc(tb.project_name)}</text>`,
    `<text x="${fmt(x + 91)}" y="${fmt(y + 5.3)}" font-size="2.8" font-weight="bold" fill="#111111">图号：${esc(tb.drawing_no)} (Rev.${esc(tb.revision)})</text>`,
    `<text x="${fmt(x + 3)}" y="${fmt(y + 13.3)}" font-size="2.6" font-weight="bold" fill="#111111">图名：${esc(tb.sheet_title)}</text>`,
    `<text x="${fmt(x + 91)}" y="${fmt(y + 13.2)}" font-size="2.3" fill="#111111">比例：${esc(tb.scale)}</text>`,
    `<text x="${fmt(x + 123)}" y="${fmt(y + 13.2)}" font-size="2.3" fill="#111111">单位：${esc(tb.unit)}</text>`,
    `<text x="${fmt(x + 3)}" y="${fmt(y + 21.2)}" font-size="2.2" fill="#222222">材质：${esc(matStr)} · ${esc(tb.projection)}</text>`,
    `<text x="${fmt(x + 91)}" y="${fmt(y + 21.2)}" font-size="2.2" fill="#111111">${esc(tb.status)} · ${esc(dateStr)}</text>`,
    `<text x="${fmt(x + 123)}" y="${fmt(y + 21.2)}" font-size="2.3" font-weight="bold" fill="#111111">第 ${tb.sheet_index} / ${tb.sheet_total} 页</text>`,
    `</g>`,
  ].join('\n');
}

export function compileDrawingSheet(raw: DrawingSheetInput): CompiledSheetResult {
  const input = drawingSheetSchema.parse(raw);
  const paper = resolvePaper(input.paper);
  const tb = input.title_block;

  const verifiedChains: CompiledSheetResult['verification']['verified_dimension_chains'] = [];
  let verifiedLinearDims = 0;

  const lines: string[] = [
    `<?xml version="1.0" encoding="UTF-8"?>`,
    `<svg xmlns="http://www.w3.org/2000/svg" width="${fmt(paper.widthMm)}mm" height="${fmt(paper.heightMm)}mm" viewBox="0 0 ${fmt(paper.widthMm)} ${fmt(paper.heightMm)}" font-family="Noto Sans CJK SC, WenQuanYi Micro Hei, DejaVu Sans, Arial, sans-serif">`,
    `<defs>`,
    `  <pattern id="hatch45" width="2.4" height="2.4" patternTransform="rotate(45 0 0)" patternUnits="userSpaceOnUse">`,
    `    <line x1="0" y1="0" x2="0" y2="2.4" stroke="#333333" stroke-width="0.14"/>`,
    `  </pattern>`,
    `  <pattern id="hatch135" width="2.4" height="2.4" patternTransform="rotate(135 0 0)" patternUnits="userSpaceOnUse">`,
    `    <line x1="0" y1="0" x2="0" y2="2.4" stroke="#333333" stroke-width="0.14"/>`,
    `  </pattern>`,
    `</defs>`,
    `<rect x="0" y="0" width="${fmt(paper.widthMm)}" height="${fmt(paper.heightMm)}" fill="#ffffff"/>`,
    // 标准工程图内外图框
    `<rect x="8" y="8" width="${fmt(paper.widthMm - 16)}" height="${fmt(paper.heightMm - 16)}" fill="none" stroke="#111111" stroke-width="0.50"/>`,
    `<rect x="10" y="10" width="${fmt(paper.widthMm - 20)}" height="${fmt(paper.heightMm - 20)}" fill="none" stroke="#111111" stroke-width="0.20"/>`,
    // 顶部图纸标题通栏
    `<line x1="10" y1="21.5" x2="${fmt(paper.widthMm - 10)}" y2="21.5" stroke="#111111" stroke-width="0.28"/>`,
    `<text x="14" y="16.2" font-size="4.0" font-weight="bold" fill="#111111">${esc(tb.project_name)} — ${esc(tb.sheet_title)}</text>`,
    `<text x="14" y="19.8" font-size="2.35" fill="#333333">${esc(tb.subtitle ?? `工程生产施工图 · 单位 ${tb.unit} · ${tb.projection} · 比例 ${tb.scale}`)}</text>`,
    `<text x="${fmt(paper.widthMm - 14)}" y="16.2" font-size="3.6" font-weight="bold" text-anchor="end" fill="#111111">${esc(tb.drawing_no)} (${tb.sheet_index}/${tb.sheet_total})</text>`,
    `<text x="${fmt(paper.widthMm - 14)}" y="19.8" font-size="2.3" text-anchor="end" fill="#222222">版本 Rev.${esc(tb.revision)} · 比例 ${esc(tb.scale)} · 单位 ${esc(tb.unit)}</text>`,
  ];

  for (const view of input.views) {
    const { ratio, label: scaleLabel } = parseScale(view.scale);
    const [ox, oy] = view.origin_mm;
    const mx = (x: number) => ox + x * ratio;
    const my = (y: number) => oy + y * ratio;
    const ms = (v: number) => v * ratio;

    lines.push(`<g id="view-${esc(view.id)}">`);
    // 视图标题栏（位于视图上方）
    lines.push(
      `<text x="${fmt(ox)}" y="${fmt(oy - 5.2)}" font-size="3.0" font-weight="bold" fill="#111111">${esc(view.title)} <tspan font-size="2.3" font-weight="normal" fill="#333333">(${esc(scaleLabel)})</tspan></text>`,
    );
    if (view.subtitle) {
      lines.push(
        `<text x="${fmt(ox)}" y="${fmt(oy - 2.2)}" font-size="2.15" fill="#333333">${esc(view.subtitle)}</text>`,
      );
    }

    for (const r of view.rects ?? []) {
      const rxAttr = r.rx ? ` rx="${fmt(ms(r.rx))}"` : '';
      lines.push(
        `<rect x="${fmt(mx(r.x))}" y="${fmt(my(r.y))}" width="${fmt(ms(r.w))}" height="${fmt(ms(r.h))}"${rxAttr} ${rectAttrs(r.style ?? 'solid')}/>`,
      );
      if (r.label) {
        lines.push(
          `<text x="${fmt(mx(r.x + r.w / 2))}" y="${fmt(my(r.y + r.h / 2) + 0.8)}" font-size="2.15" text-anchor="middle" fill="#222222">${esc(r.label)}</text>`,
        );
      }
    }

    for (const f of view.fretwork ?? []) {
      const cells = f.cells ?? Math.max(2, Math.round(f.w / Math.max(f.h, 40)));
      lines.push(drawFretworkBand(mx(f.x), my(f.y), ms(f.w), ms(f.h), cells, f.motif));
    }

    for (const p of view.polylines ?? []) {
      const pts = p.points.map(([px, py]) => `${fmt(mx(px))},${fmt(my(py))}`).join(' ');
      const tag = p.closed ? 'polygon' : 'polyline';
      lines.push(`<${tag} points="${pts}" ${rectAttrs(p.style ?? 'solid')}/>`);
    }

    for (const l of view.lines ?? []) {
      lines.push(
        `<line x1="${fmt(mx(l.x1))}" y1="${fmt(my(l.y1))}" x2="${fmt(mx(l.x2))}" y2="${fmt(my(l.y2))}" ${lineAttrs(l.style ?? 'solid')}/>`,
      );
    }

    for (const c of view.circles ?? []) {
      const cx = mx(c.cx);
      const cy = my(c.cy);
      const r = Math.max(0.45, ms(c.r));
      lines.push(
        `<circle cx="${fmt(cx)}" cy="${fmt(cy)}" r="${fmt(r)}" fill="none" ${lineAttrs(c.style ?? 'solid')}/>`,
      );
      if (c.crosshair ?? true) {
        const ext = r + 1.4;
        lines.push(
          `<line x1="${fmt(cx - ext)}" y1="${fmt(cy)}" x2="${fmt(cx + ext)}" y2="${fmt(cy)}" ${lineAttrs('center')}/>`,
          `<line x1="${fmt(cx)}" y1="${fmt(cy - ext)}" x2="${fmt(cx)}" y2="${fmt(cy + ext)}" ${lineAttrs('center')}/>`,
        );
      }
      if (c.label) {
        lines.push(
          `<text x="${fmt(cx + r + 1.2)}" y="${fmt(cy - 0.6)}" font-size="2.1" fill="#111111">${esc(c.label)}</text>`,
        );
      }
    }

    for (const e of view.ellipses ?? []) {
      const cx = mx(e.cx);
      const cy = my(e.cy);
      const rx = Math.max(0.45, ms(e.rx));
      const ry = Math.max(0.45, ms(e.ry));
      lines.push(
        `<ellipse cx="${fmt(cx)}" cy="${fmt(cy)}" rx="${fmt(rx)}" ry="${fmt(ry)}" fill="none" ${lineAttrs(e.style ?? 'solid')}/>`,
      );
      if (e.crosshair ?? true) {
        lines.push(
          `<line x1="${fmt(cx - rx - 1.4)}" y1="${fmt(cy)}" x2="${fmt(cx + rx + 1.4)}" y2="${fmt(cy)}" ${lineAttrs('center')}/>`,
          `<line x1="${fmt(cx)}" y1="${fmt(cy - ry - 1.4)}" x2="${fmt(cx)}" y2="${fmt(cy + ry + 1.4)}" ${lineAttrs('center')}/>`,
        );
      }
      if (e.label) {
        lines.push(
          `<text x="${fmt(cx + rx + 1.2)}" y="${fmt(cy - 0.6)}" font-size="2.1" fill="#111111">${esc(e.label)}</text>`,
        );
      }
    }

    for (const a of view.arcs ?? []) {
      const cx = mx(a.cx);
      const cy = my(a.cy);
      const r = ms(a.r);
      const sRad = (a.start_deg * Math.PI) / 180;
      const eRad = (a.end_deg * Math.PI) / 180;
      const x1 = cx + r * Math.cos(sRad);
      const y1 = cy + r * Math.sin(sRad);
      const x2 = cx + r * Math.cos(eRad);
      const y2 = cy + r * Math.sin(eRad);
      const largeArc = Math.abs(a.end_deg - a.start_deg) > 180 ? 1 : 0;
      const sweep = a.end_deg >= a.start_deg ? 1 : 0;
      lines.push(
        `<path d="M ${fmt(x1)} ${fmt(y1)} A ${fmt(r)} ${fmt(r)} 0 ${largeArc} ${sweep} ${fmt(x2)} ${fmt(y2)}" fill="none" ${lineAttrs(a.style ?? 'thin')}/>`,
      );
    }

    for (const p of view.paths ?? []) {
      const st = p.style ?? 'solid';
      const baseStroke = st === 'solid' ? 0.35 : st === 'medium' ? 0.25 : 0.18;
      const scaledStroke = fmt(baseStroke / ratio);
      const dashAttr =
        st === 'hidden'
          ? ` stroke-dasharray="${fmt(1.8 / ratio)},${fmt(1.0 / ratio)}"`
          : st === 'center'
            ? ` stroke-dasharray="${fmt(4.0 / ratio)},${fmt(1.2 / ratio)},${fmt(0.8 / ratio)},${fmt(1.2 / ratio)}"`
            : '';
      const fillAttr =
        st === 'hatch45'
          ? 'fill="url(#hatch45)"'
          : st === 'hatch135'
            ? 'fill="url(#hatch135)"'
            : st === 'fill_light'
              ? 'fill="#f5f5f5"'
              : st === 'fill_dark'
                ? 'fill="#e2e2e2"'
                : 'fill="none"';
      lines.push(
        `<g transform="translate(${fmt(ox)}, ${fmt(oy)}) scale(${fmt(ratio)})"><path d="${esc(p.d)}" ${fillAttr} stroke="#111111" stroke-width="${scaledStroke}"${dashAttr}/></g>`,
      );
    }

    for (const rd of view.radial_dims ?? []) {
      const cx = mx(rd.cx);
      const cy = my(rd.cy);
      const rPaper = ms(rd.r);
      const rad = (rd.angle_deg * Math.PI) / 180;
      const ex = cx + rPaper * Math.cos(rad);
      const ey = cy - rPaper * Math.sin(rad);
      const lx = ex + (Math.cos(rad) >= 0 ? 4.5 : -4.5);
      const ly = ey + (Math.sin(rad) >= 0 ? -3.5 : 3.5);
      const dir = Math.cos(rad) >= 0 ? 1 : -1;
      const prefix = rd.mode === 'diameter' ? 'Ø' : 'R';
      const val = rd.mode === 'diameter' ? rd.r * 2 : rd.r;
      const lbl = rd.label ?? `${prefix}${fmt(val)}`;
      lines.push(
        `<circle cx="${fmt(ex)}" cy="${fmt(ey)}" r="0.55" fill="#111111"/>`,
        `<polyline points="${fmt(cx)},${fmt(cy)} ${fmt(ex)},${fmt(ey)} ${fmt(lx)},${fmt(ly)} ${fmt(lx + dir * 6.5)},${fmt(ly)}" fill="none" stroke="#111111" stroke-width="0.18"/>`,
        `<text x="${fmt(lx + dir * 3.2)}" y="${fmt(ly - 0.8)}" font-size="2.25" text-anchor="middle" fill="#111111">${esc(lbl)}</text>`,
      );
      verifiedLinearDims += 1;
    }

    for (const sm of view.section_marks ?? []) {
      if (sm.orientation === 'vertical') {
        const sx = mx(sm.position_model);
        const y1 = my(sm.start_model);
        const y2 = my(sm.end_model);
        const dir = sm.direction === 'left' ? -1 : 1;
        lines.push(
          `<line x1="${fmt(sx)}" y1="${fmt(y1)}" x2="${fmt(sx)}" y2="${fmt(y2)}" ${lineAttrs('center')}/>`,
          `<line x1="${fmt(sx)}" y1="${fmt(y1 - 3)}" x2="${fmt(sx)}" y2="${fmt(y1 + 2)}" stroke="#111111" stroke-width="0.55"/>`,
          `<line x1="${fmt(sx)}" y1="${fmt(y2 - 2)}" x2="${fmt(sx)}" y2="${fmt(y2 + 3)}" stroke="#111111" stroke-width="0.55"/>`,
          `<line x1="${fmt(sx)}" y1="${fmt(y1 - 2)}" x2="${fmt(sx + dir * 5)}" y2="${fmt(y1 - 2)}" stroke="#111111" stroke-width="0.30"/>`,
          `<line x1="${fmt(sx)}" y1="${fmt(y2 + 2)}" x2="${fmt(sx + dir * 5)}" y2="${fmt(y2 + 2)}" stroke="#111111" stroke-width="0.30"/>`,
          drawArrow(sx + dir * 5, y1 - 2, dir > 0 ? 'right' : 'left', 1.8, 0.55),
          drawArrow(sx + dir * 5, y2 + 2, dir > 0 ? 'right' : 'left', 1.8, 0.55),
          `<text x="${fmt(sx + dir * 6.2)}" y="${fmt(y1 - 1.2)}" font-size="2.8" font-weight="bold" fill="#111111">${esc(sm.label)}</text>`,
          `<text x="${fmt(sx + dir * 6.2)}" y="${fmt(y2 + 2.8)}" font-size="2.8" font-weight="bold" fill="#111111">${esc(sm.label)}</text>`,
        );
      } else {
        const sy = my(sm.position_model);
        const x1 = mx(sm.start_model);
        const x2 = mx(sm.end_model);
        const dir = sm.direction === 'up' ? -1 : 1;
        lines.push(
          `<line x1="${fmt(x1)}" y1="${fmt(sy)}" x2="${fmt(x2)}" y2="${fmt(sy)}" ${lineAttrs('center')}/>`,
          `<line x1="${fmt(x1 - 3)}" y1="${fmt(sy)}" x2="${fmt(x1 + 2)}" y2="${fmt(sy)}" stroke="#111111" stroke-width="0.55"/>`,
          `<line x1="${fmt(x2 - 2)}" y1="${fmt(sy)}" x2="${fmt(x2 + 3)}" y2="${fmt(sy)}" stroke="#111111" stroke-width="0.55"/>`,
          `<line x1="${fmt(x1 - 2)}" y1="${fmt(sy)}" x2="${fmt(x1 - 2)}" y2="${fmt(sy + dir * 5)}" stroke="#111111" stroke-width="0.30"/>`,
          `<line x1="${fmt(x2 + 2)}" y1="${fmt(sy)}" x2="${fmt(x2 + 2)}" y2="${fmt(sy + dir * 5)}" stroke="#111111" stroke-width="0.30"/>`,
          drawArrow(x1 - 2, sy + dir * 5, dir > 0 ? 'down' : 'up', 1.8, 0.55),
          drawArrow(x2 + 2, sy + dir * 5, dir > 0 ? 'down' : 'up', 1.8, 0.55),
          `<text x="${fmt(x1 - 3.5)}" y="${fmt(sy + dir * 7.8)}" font-size="2.8" font-weight="bold" fill="#111111">${esc(sm.label)}</text>`,
          `<text x="${fmt(x2 + 1.5)}" y="${fmt(sy + dir * 7.8)}" font-size="2.8" font-weight="bold" fill="#111111">${esc(sm.label)}</text>`,
        );
      }
    }

    for (const ld of view.linear_dims ?? []) {
      const spanModel = Math.abs(ld.end - ld.start);
      if (ld.value != null && Math.abs(ld.value - spanModel) > 1.0) {
        throw new ToolFailure(
          'DIMENSION_VALUE_MISMATCH',
          `View "${view.id}" linear_dim value ${ld.value} does not match coordinate span ${spanModel}`,
        );
      }
      const numVal = ld.value ?? spanModel;
      const textLabel = ld.label ?? `${ld.prefix ?? ''}${fmt(numVal)}${ld.suffix ?? ''}`;
      if (ld.orientation === 'horizontal') {
        const anchorY = my(ld.anchor);
        const dimY = anchorY + ld.offset_mm;
        lines.push(drawHorizontalDim(mx(ld.start), mx(ld.end), anchorY, dimY, textLabel));
      } else {
        const anchorX = mx(ld.anchor);
        const dimX = anchorX + ld.offset_mm;
        lines.push(drawVerticalDim(my(ld.start), my(ld.end), anchorX, dimX, textLabel));
      }
      verifiedLinearDims += 1;
    }

    for (const cd of view.chain_dims ?? []) {
      const sum = cd.segments.reduce((acc, s) => acc + s, 0);
      if (cd.expected_total != null && Math.abs(sum - cd.expected_total) > 0.5) {
        throw new ToolFailure(
          'DIMENSION_CHAIN_MISMATCH',
          `View "${view.id}" ${cd.orientation} chain_dim segments [${cd.segments.join(', ')}] sum to ${sum}, expected ${cd.expected_total}`,
        );
      }
      verifiedChains.push({
        view_id: view.id,
        orientation: cd.orientation,
        segments: cd.segments,
        sum_mm: sum,
        expected_total_mm: cd.expected_total,
      });

      let cursor = cd.start;
      for (let i = 0; i < cd.segments.length; i++) {
        const seg = cd.segments[i];
        const next = cursor + seg;
        const lbl = cd.labels?.[i] ?? fmt(seg);
        if (cd.orientation === 'horizontal') {
          const anchorY = my(cd.anchor);
          lines.push(
            drawHorizontalDim(mx(cursor), mx(next), anchorY, anchorY + cd.offset_mm, lbl, i),
          );
        } else {
          const anchorX = mx(cd.anchor);
          lines.push(
            drawVerticalDim(my(cursor), my(next), anchorX, anchorX + cd.offset_mm, lbl, i),
          );
        }
        cursor = next;
      }

      if (cd.show_total) {
        const totalVal = cd.expected_total ?? sum;
        const sign = cd.offset_mm >= 0 ? 1 : -1;
        const totalOff = cd.total_offset_mm ?? cd.offset_mm + sign * 6.2;
        if (cd.orientation === 'horizontal') {
          const anchorY = my(cd.anchor);
          lines.push(
            drawHorizontalDim(
              mx(cd.start),
              mx(cd.start + sum),
              anchorY,
              anchorY + totalOff,
              fmt(totalVal),
            ),
          );
        } else {
          const anchorX = mx(cd.anchor);
          lines.push(
            drawVerticalDim(
              my(cd.start),
              my(cd.start + sum),
              anchorX,
              anchorX + totalOff,
              fmt(totalVal),
            ),
          );
        }
      }
    }

    for (const el of view.elevation_ladders ?? []) {
      const baseX = mx(el.x_model) + el.offset_mm;
      const datumY = my(el.datum_y_model);
      for (const lv of el.levels) {
        const ly = el.direction === 'up' ? datumY - ms(lv.elevation) : datumY + ms(lv.elevation);
        const txt = lv.label ?? (lv.elevation === 0 ? '±0.00 (基准)' : `+${fmt(lv.elevation)}`);
        lines.push(
          `<line x1="${fmt(baseX - 2.2)}" y1="${fmt(ly)}" x2="${fmt(baseX + 5.5)}" y2="${fmt(ly)}" stroke="#222222" stroke-width="0.16"/>`,
          `<polygon points="${fmt(baseX)},${fmt(ly)} ${fmt(baseX - 1.4)},${fmt(ly - 1.5)} ${fmt(baseX + 1.4)},${fmt(ly - 1.5)}" fill="none" stroke="#111111" stroke-width="0.18"/>`,
          `<text x="${fmt(baseX + 1.8)}" y="${fmt(ly - 0.6)}" font-size="2.1" fill="#111111">${esc(txt)}</text>`,
        );
      }
    }

    for (const pt of view.part_tags ?? []) {
      const px = mx(pt.x);
      const py = my(pt.y);
      const dx = pt.dx_mm ?? 6.5;
      const dy = pt.dy_mm ?? -4.5;
      const tx = px + dx;
      const ty = py + dy;
      const dir = dx >= 0 ? 1 : -1;
      const fullText = pt.label ? `${pt.tag} ${pt.label}` : pt.tag;
      const boxW = Math.max(7.5, fullText.length * 1.9 + 2.4);
      lines.push(
        `<circle cx="${fmt(px)}" cy="${fmt(py)}" r="0.65" fill="#111111"/>`,
        `<polyline points="${fmt(px)},${fmt(py)} ${fmt(tx)},${fmt(ty)} ${fmt(tx + dir * 2.5)},${fmt(ty)}" fill="none" stroke="#111111" stroke-width="0.18"/>`,
        `<rect x="${fmt(dir >= 0 ? tx + 2.5 : tx - 2.5 - boxW)}" y="${fmt(ty - 2.4)}" width="${fmt(boxW)}" height="3.8" rx="0.6" fill="#ffffff" stroke="#111111" stroke-width="0.20"/>`,
        `<text x="${fmt(dir >= 0 ? tx + 2.5 + boxW / 2 : tx - 2.5 - boxW / 2)}" y="${fmt(ty + 0.25)}" font-size="2.15" font-weight="bold" text-anchor="middle" fill="#111111">${esc(fullText)}</text>`,
      );
    }

    for (const t of view.texts ?? []) {
      const tx = mx(t.x);
      const ty = my(t.y);
      const sz = t.size_mm ?? 2.3;
      const weight = t.bold ? ' font-weight="bold"' : '';
      const anchor = t.align ? ` text-anchor="${t.align}"` : '';
      const rot = t.rotate ? ` transform="rotate(${fmt(t.rotate)} ${fmt(tx)} ${fmt(ty)})"` : '';
      if (t.box) {
        const bw = Math.max(8, t.text.length * sz * 0.72 + 2.4);
        const bx = t.align === 'middle' ? tx - bw / 2 : t.align === 'end' ? tx - bw : tx - 1.2;
        lines.push(
          `<rect x="${fmt(bx)}" y="${fmt(ty - sz - 0.5)}" width="${fmt(bw)}" height="${fmt(sz + 1.6)}" fill="#ffffff" stroke="#222222" stroke-width="0.18"/>`,
        );
      }
      lines.push(
        `<text x="${fmt(tx)}" y="${fmt(ty)}" font-size="${fmt(sz)}"${weight}${anchor}${rot} fill="#111111">${esc(t.text)}</text>`,
      );
    }

    lines.push('</g>');
  }

  for (const detail of input.details) {
    lines.push(renderDetailBlock(detail));
  }

  for (const table of input.tables) {
    lines.push(renderTable(table));
  }

  for (const noteBox of input.note_boxes) {
    lines.push(renderNoteBox(noteBox));
  }

  lines.push(renderTitleBlock(paper, tb));
  lines.push('</svg>');

  const svgBytes = Buffer.from(lines.join('\n'), 'utf8');
  // 校验生成的物理图纸 SVG 严格满足自包含与纸张尺寸要求。
  selfContainedSvg(svgBytes, paper);

  const bomRows = input.tables.reduce((acc, t) => acc + t.rows.length, 0);
  return {
    svgBytes,
    paper,
    verification: {
      drawing_no: tb.drawing_no,
      sheet_title: tb.sheet_title,
      page_size_mm: [paper.widthMm, paper.heightMm],
      view_count: input.views.length,
      detail_count: input.details.length,
      table_count: input.tables.length,
      note_box_count: input.note_boxes.length,
      verified_dimension_chains: verifiedChains,
      verified_linear_dims: verifiedLinearDims,
      bom_row_count: bomRows,
    },
  };
}
