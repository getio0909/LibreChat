import { useState } from 'react';
import { FolderDown } from 'lucide-react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { QueryKeys, MutationKeys, dataService } from 'librechat-data-provider';
import { Button, OGDialog, OGDialogTemplate, OGDialogTrigger, Spinner } from '@librechat/client';
import { useGetStartupConfig } from '~/data-provider';
import { useLocalize } from '~/hooks';

export function CADExports({ enabled, row = false }: { enabled: boolean; row?: boolean }) {
  const localize = useLocalize();
  const [open, setOpen] = useState(false);
  const query = useQuery({
    queryKey: [QueryKeys.cadExports],
    queryFn: dataService.getCADExports,
    enabled: enabled && open,
  });
  const download = useMutation({
    mutationKey: [MutationKeys.cadDownload],
    mutationFn: dataService.downloadCADExport,
    onSuccess: (blob, filename) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    },
  });
  if (!enabled) return null;
  return (
    <OGDialog open={open} onOpenChange={setOpen}>
      <OGDialogTrigger asChild>
        <Button
          variant="ghost"
          size={row ? 'row' : 'icon'}
          aria-label={localize('com_ui_cad_exports')}
        >
          <FolderDown className="h-5 w-5" aria-hidden="true" />
          {row && localize('com_ui_cad_exports')}
        </Button>
      </OGDialogTrigger>
      <OGDialogTemplate
        title={localize('com_ui_cad_exports')}
        main={
          <div className="flex flex-col gap-3">
            <p>{localize('com_ui_cad_description')}</p>
            <Button
              variant="outline"
              onClick={() => void query.refetch()}
              disabled={query.isFetching}
            >
              {localize('com_ui_cad_refresh')}
            </Button>
            {query.isLoading && <Spinner />}
            {query.isError && <p role="alert">{localize('com_ui_cad_load_error')}</p>}
            {query.isSuccess && query.data.length === 0 && <p>{localize('com_ui_cad_empty')}</p>}
            <ul className="flex flex-col gap-2">
              {query.data?.map((file) => (
                <li key={file.filename} className="flex items-center justify-between gap-3">
                  <span className="min-w-0 truncate">{file.filename}</span>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={download.isLoading}
                    onClick={() => download.mutate(file.filename)}
                  >
                    {localize('com_ui_download')}
                  </Button>
                </li>
              ))}
            </ul>
            {download.isError && <p role="alert">{localize('com_ui_download_error')}</p>}
          </div>
        }
      />
    </OGDialog>
  );
}

export default function CAD({ row = false }: { row?: boolean }) {
  const { data: startup } = useGetStartupConfig();
  return <CADExports enabled={startup?.interface?.cadFiles === true} row={row} />;
}
