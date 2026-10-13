import yaml from 'js-yaml';
import path from 'node:path';
import { constants } from 'node:fs';
import { configSchema } from 'librechat-data-provider';
import type { Request, RequestHandler, Response } from 'express';
import type { TCustomConfig } from 'librechat-data-provider';
import type * as Files from 'node:fs/promises';

interface CADDeps {
  files: Pick<typeof Files, 'readdir' | 'lstat' | 'open'>;
  reportError: (message: string) => void;
}

// 仅公开常见 CAD 和图片导出格式，避免把运行日志或内部配置列为下载文件。
const exportExtension =
  /\.(fcstd|step|stp|stl|obj|ply|blend|gltf|glb|png|jpg|jpeg|webp|svg|dxf|igs|iges|pdf)$/i;
const filenamePattern = /^[^\\/]+$/;

function hasCode(error: unknown, code: string): boolean {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === code;
}

export function configureCADDeployment(
  source: string,
  owners: Record<'admin' | 'user', string>,
): string {
  const config = configSchema
    .pick({ cadExports: true, mcpSettings: true })
    .passthrough()
    .parse(yaml.load(source));
  const workspaces = config.cadExports.workspaces;
  for (const account of ['admin', 'user'] as const) {
    if (!/^[a-f0-9]{24}$/.test(owners[account])) throw new Error('Invalid CAD workspace owner.');
  }
  if (workspaces.shared) workspaces.shared.ownerIds = Object.values(owners);
  config.mcpSettings = configSchema.shape.mcpSettings.parse({
    ...config.mcpSettings,
    allowedAddresses: [
      ...(config.mcpSettings?.allowedAddresses ?? []),
      ...Object.values(owners).flatMap((id) => [`freecad-${id}:8000`, `blender-${id}:10101`]),
    ],
  });
  return yaml.dump(config);
}

/** 工作目录只允许配置的数据库用户标识访问，下载只接受根目录中的普通导出文件。 */
export function createCADController({ files, reportError }: CADDeps): {
  list: RequestHandler;
  download: RequestHandler;
} {
  function rootFor(req: Request): string | undefined {
    // 认证与配置中间件提供此只读上下文，不从请求参数读取用户身份或目录。
    const context = req as Request & {
      user?: { id: string };
      config?: { config: Partial<TCustomConfig> };
    };
    const userId = context.user?.id;
    if (!userId) return;
    return Object.values(context.config?.config.cadExports?.workspaces ?? {}).find((workspace) =>
      workspace?.ownerIds?.includes(userId),
    )?.path;
  }
  function error(res: Response, status: number, code: string) {
    res.status(status).json({ code });
  }
  return {
    list: async (req, res) => {
      const root = rootFor(req);
      if (!root) return error(res, 403, 'CAD_WORKSPACE_UNAVAILABLE');
      try {
        const entries = await files.readdir(root, { withFileTypes: true });
        const records = await Promise.all(
          entries
            .filter((entry) => entry.isFile() && exportExtension.test(entry.name))
            .map(async (entry) => {
              try {
                const stat = await files.lstat(path.join(root, entry.name));
                return stat.isFile()
                  ? { filename: entry.name, bytes: stat.size, updatedAt: stat.mtime.toISOString() }
                  : null;
              } catch (error) {
                if (hasCode(error, 'ENOENT')) return null;
                throw error;
              }
            }),
        );
        res.setHeader('Cache-Control', 'no-store');
        res.json(records.filter((record) => record !== null));
      } catch {
        reportError('CAD export listing failed.');
        error(res, 500, 'CAD_EXPORT_UNAVAILABLE');
      }
    },
    download: async (req, res) => {
      const root = rootFor(req);
      if (!root) return error(res, 403, 'CAD_WORKSPACE_UNAVAILABLE');
      const filename = req.params.filename;
      if (
        typeof filename !== 'string' ||
        !filenamePattern.test(filename) ||
        Array.from(filename).some((character) => character.charCodeAt(0) < 32) ||
        !exportExtension.test(filename)
      )
        return error(res, 404, 'CAD_FILE_NOT_FOUND');
      let handle: Files.FileHandle | undefined;
      try {
        if (!(await files.lstat(path.join(root, filename))).isFile())
          return error(res, 404, 'CAD_FILE_NOT_FOUND');
        // 根目录由配置固定；拒绝符号链接，防止 CAD 进程通过文件替换读取其他路径。
        const file = await files.open(
          path.join(root, filename),
          constants.O_RDONLY | constants.O_NOFOLLOW,
        );
        handle = file;
        const stat = await file.stat();
        if (!stat.isFile()) {
          await file.close();
          return error(res, 404, 'CAD_FILE_NOT_FOUND');
        }
        const stream = file.createReadStream();
        handle = undefined;
        stream.on('error', () => {
          reportError('CAD export download failed.');
          res.destroy();
        });
        res.on('close', () => stream.destroy());
        res.setHeader('Cache-Control', 'no-store');
        res.setHeader('Content-Type', 'application/octet-stream');
        res.setHeader(
          'Content-Disposition',
          `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`,
        );
        stream.pipe(res);
      } catch (failure) {
        if (handle) await handle.close();
        if (hasCode(failure, 'ENOENT') || hasCode(failure, 'ELOOP')) {
          return error(res, 404, 'CAD_FILE_NOT_FOUND');
        }
        reportError('CAD export download failed.');
        error(res, 500, 'CAD_EXPORT_UNAVAILABLE');
      }
    },
  };
}
