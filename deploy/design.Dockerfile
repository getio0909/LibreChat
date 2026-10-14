FROM node:24.16.0-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends fontconfig fonts-dejavu-core fonts-noto-cjk librsvg2-bin poppler-utils util-linux qpdf && fc-cache -f && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV XDG_CACHE_HOME=/tmp/.cache MALLOC_ARENA_MAX=1 RAYON_NUM_THREADS=1 OMP_NUM_THREADS=1
COPY deploy/design/package.json deploy/design/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY deploy/design/config.ts deploy/design/files.ts deploy/design/download.ts deploy/design/images.ts deploy/design/mcp.ts deploy/design/pdf.ts deploy/design/parse.ts deploy/design/prune.ts deploy/design/svg.ts deploy/design/sheet.ts deploy/design/render.ts deploy/design/server.ts ./
CMD ["node", "server.ts"]
