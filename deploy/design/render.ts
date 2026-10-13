import { execFile } from 'node:child_process';
import type { ExecFileException } from 'node:child_process';
import { ToolFailure } from './config.ts';

export function render(
  command: 'rsvg-convert' | 'pdftoppm',
  args: string[],
  input: Buffer,
  maxBytes: number,
  signal: AbortSignal,
): Promise<Buffer> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    let result: { error: ExecFileException | null; stdout: Buffer } | undefined;
    // 通过标准输入传入内容，不设置 SVG 基准路径，也不经过命令解释器。
    const child = execFile(
      command,
      args,
      { encoding: 'buffer', maxBuffer: maxBytes, signal, killSignal: 'SIGKILL' },
      (error, stdout) => {
        result = { error, stdout };
      },
    );
    // 中止回调可能先于进程退出；只有完全退出后才能释放工作名额。
    child.once('close', () => {
      if (signal.aborted) return reject(new ToolFailure('REQUEST_CANCELLED'));
      if (result?.error?.code === 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER')
        return reject(new ToolFailure('FILE_TOO_LARGE'));
      if (result?.error?.code === 'ENOENT')
        return reject(new ToolFailure('PDF_RENDERER_UNAVAILABLE'));
      if (!result || result.error) return reject(new ToolFailure('DRAWING_RENDER_FAILED'));
      resolve(result.stdout);
    });
    // 提前退出的渲染进程可能关闭输入；最终错误由进程回调统一处理。
    child.stdin?.on('error', () => {});
    child.stdin?.end(input);
  });
}
