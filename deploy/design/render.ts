import { execFile } from 'node:child_process';
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
    // 通过标准输入传入内容，不设置 SVG 基准路径，也不经过命令解释器。
    const child = execFile(
      command,
      args,
      { encoding: 'buffer', maxBuffer: maxBytes, signal, killSignal: 'SIGKILL' },
      (error, stdout) => {
        if (signal.aborted) return reject(new ToolFailure('REQUEST_CANCELLED'));
        if (error?.code === 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER')
          return reject(new ToolFailure('FILE_TOO_LARGE'));
        if (error?.code === 'ENOENT') return reject(new ToolFailure('PDF_RENDERER_UNAVAILABLE'));
        if (error) return reject(new ToolFailure('DRAWING_RENDER_FAILED'));
        resolve(stdout);
      },
    );
    // 提前退出的渲染进程可能关闭输入；最终错误由进程回调统一处理。
    child.stdin?.on('error', () => {});
    child.stdin?.end(input);
  });
}
