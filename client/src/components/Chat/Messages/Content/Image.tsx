import React, { useState, useRef, useMemo, useEffect } from 'react';
import { Skeleton } from '@librechat/client';
import { apiBaseUrl } from 'librechat-data-provider';
import type { TFile } from 'librechat-data-provider';
import { formatGeneratedImageFilename } from '~/utils/downloadFile';
import { cn, toAbsoluteFilePath } from '~/utils';
import useLocalize from '~/hooks/useLocalize';
import DialogImage from './DialogImage';

/** Max display height for chat images (Tailwind JIT class) */
export const IMAGE_MAX_H = 'max-h-[45vh]' as const;
/** Matches the `max-w-lg` Tailwind class on the wrapper button (32rem = 512px at 16px base) */
const IMAGE_MAX_W_PX = 512;

/** Caches image dimensions by src so remounts can reserve space */
const dimensionCache = new Map<string, { width: number; height: number }>();
/** Tracks URLs that have been fully painted — skip skeleton on remount */
const paintedUrls = new Set<string>();

/** Test-only: resets module-level caches */
export function _resetImageCaches(): void {
  dimensionCache.clear();
  paintedUrls.clear();
}

function computeHeightStyle(w: number, h: number): React.CSSProperties {
  return { height: `min(45vh, ${(h / w) * 100}vw, ${(h / w) * IMAGE_MAX_W_PX}px)` };
}

const Image = ({
  imagePath,
  altText,
  className,
  alignRight = false,
  args,
  width,
  height,
  fileId,
  fileContext,
  fileType,
}: {
  imagePath: string;
  altText: string;
  className?: string;
  alignRight?: boolean;
  args?: {
    prompt?: string;
    quality?: 'low' | 'medium' | 'high';
    size?: string;
    style?: string;
    [key: string]: unknown;
  };
  width?: number;
  height?: number;
  fileId?: string;
  fileContext?: TFile['context'];
  fileType?: string;
}) => {
  const localize = useLocalize();
  const [isOpen, setIsOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  // 依据持久化 File 上下文判断产图来源，避免用户上传文件名触发误改名
  const displayAltText = formatGeneratedImageFilename(
    { filename: altText, file_id: fileId, context: fileContext, type: fileType },
    localize,
  );
  const downloadFilename = displayAltText || 'image.png';

  /** Root-relative server paths (`/images/...` static, `/api/...` downloads and
   *  share routes) are resolved against the API base so they load under a
   *  subpath deployment. */
  const absoluteImageUrl = useMemo(() => toAbsoluteFilePath(imagePath, apiBaseUrl()), [imagePath]);

  const downloadImage = async () => {
    try {
      const response = await fetch(absoluteImageUrl);
      if (!response.ok) {
        throw new Error(`Failed to fetch image: ${response.status}`);
      }

      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);

      const link = document.createElement('a');
      link.href = url;
      link.download = downloadFilename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);

      window.URL.revokeObjectURL(url);
    } catch (error) {
      console.error('Download failed:', error);
      const link = document.createElement('a');
      link.href = absoluteImageUrl;
      link.download = downloadFilename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    }
  };

  useEffect(() => {
    if (width && height && absoluteImageUrl) {
      dimensionCache.set(absoluteImageUrl, { width, height });
    }
  }, [absoluteImageUrl, width, height]);

  const dims = width && height ? { width, height } : dimensionCache.get(absoluteImageUrl);
  const hasDimensions = !!(dims?.width && dims?.height);
  const heightStyle = hasDimensions ? computeHeightStyle(dims.width, dims.height) : undefined;
  const showSkeleton = hasDimensions && !paintedUrls.has(absoluteImageUrl);

  return (
    <div className={alignRight ? 'ml-auto' : undefined}>
      <button
        ref={triggerRef}
        type="button"
        aria-label={`View ${displayAltText} in dialog`}
        aria-haspopup="dialog"
        onClick={() => setIsOpen(true)}
        className={cn(
          'border-border-light text-text-secondary-alt relative mt-1 w-full max-w-lg cursor-pointer overflow-hidden rounded-lg border shadow-md transition-shadow',
          'focus-visible:ring-text-primary focus-visible:ring-offset-surface-primary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-offset-2',
          className,
        )}
        style={heightStyle}
      >
        {showSkeleton && <Skeleton className="absolute inset-0" aria-hidden="true" />}
        <img
          alt={displayAltText}
          src={absoluteImageUrl}
          onLoad={() => paintedUrls.add(absoluteImageUrl)}
          className={cn(
            'relative block text-transparent',
            hasDimensions
              ? 'size-full object-contain'
              : cn('h-auto w-auto max-w-full', IMAGE_MAX_H),
          )}
        />
      </button>
      <DialogImage
        isOpen={isOpen}
        onOpenChange={setIsOpen}
        src={absoluteImageUrl}
        downloadImage={downloadImage}
        args={args}
        triggerRef={triggerRef}
        title={displayAltText || undefined}
      />
    </div>
  );
};

export default Image;
