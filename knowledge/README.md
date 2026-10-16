# zybc-knowledge (庄严宝刹设计知识库)

Canonical asset & reference knowledge base for the **Alitan Stone / 庄严宝刹** Buddhist & stone spatial design studio.

## 1. Purpose / 用途

This repository stores:
- One canonical YAML record per design reference asset (`records/<asset_id>.yaml`).
- Pre-normalized, EXIF-stripped sRGB derivatives (`assets/ref2048/<asset_id>.jpg` for image generation up to 2048 px, and `assets/thumb512/<asset_id>.webp` for search & UI previews up to 512 px).
- Proprietary stone catalog (`catalog/materials.yaml`) and curated style packs (`catalog/styles.yaml`).
- Deterministic build and validation tooling (`tools/`) producing `dist/index.jsonl`, `dist/views/*.md`, and `dist/originals-v1.tar.zst`.

## 2. Repository Layout / 目录结构

```text
zybc-knowledge/
├── README.md
├── catalog/
│   ├── materials.yaml          # Stone catalog + aliases (敦煌金/天川金, 敦煌壁/梵高皇, 金镶玉/梦幻庄园, 蓝金灰)
│   └── styles.yaml             # Verified style references and packs
├── schema/
│   └── record.schema.json      # JSON Schema (Draft 2020-12) for records/*.yaml
├── records/
│   └── <asset_id>.yaml         # Canonical metadata record per asset (16-char SHA256 prefix)
├── assets/
│   ├── ref2048/<asset_id>.jpg  # <=2048 px long edge, EXIF transposed & stripped, sRGB JPEG (q=85)
│   └── thumb512/<asset_id>.webp# <=512 px long edge, EXIF transposed & stripped, sRGB WebP (q=80)
├── tools/
│   ├── ingest.py               # Compute IDs, normalize derivatives, create draft records + Gemini schema
│   ├── validate.py             # CI validator (schema, hashes, catalog refs, path & secret checks)
│   └── build.py                # Build dist/index.jsonl, dist/views/*.md, dist/originals-v1.tar.zst
├── tests/
│   └── test_kb.py              # Pytest suite for ID derivation, CMYK/EXIF normalization, validator
└── dist/                       # Gitignored build outputs (index.jsonl, views/*.md, originals archive)
```

## 3. How to Add Images / 新增素材流程

1. **Ingest new image(s) as `draft`**:
   ```bash
   py313 python tools/ingest.py path/to/new_image.jpg --source own_product
   ```
   This derives `<asset_id>` (`sha256[:16]` of original bytes), generates `assets/ref2048/<asset_id>.jpg` and `assets/thumb512/<asset_id>.webp` (transposing EXIF orientation, converting CMYK/ICC to sRGB, stripping EXIF), and writes `records/<asset_id>.yaml` with `review.status: draft`.
2. **Label & review**:
    Export the Gemini structured labeling schema via `py313 python tools/ingest.py --print-gemini-schema`, populate visual fields (`library_type`, `studio_roles`, `summary_zh`, `materials`, `colors`, `style`, `material_ids`, `quality`), inspect visually, and update `review.status` to `reviewed`.
3. **Validate & build**:
   ```bash
   py313 python -m pytest
   py313 python tools/validate.py
   py313 python tools/build.py
   ```

## 4. Release Pinning in Studio / 版本发布与工作台锁定

- **Release tag scheme**: `kb-vYYYY.MM.DD` (e.g., `kb-v2026.10.16`).
- **Untouched originals archive**: Attached to the release as `dist/originals-v1.tar.zst` alongside `SHA256SUMS` and `originals-manifest.json`.
- **Studio pinning**: The studio service pins a specific release tag (`KB_RELEASE_TAG=kb-vYYYY.MM.DD`), mounts or checks out that tag read-only, loads `dist/index.jsonl` + `catalog/*.yaml` at startup, and serves `assets/thumb512/<id>.webp` for search cards and `assets/ref2048/<id>.jpg` for reference-guided generation.
