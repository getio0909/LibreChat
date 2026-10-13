import axios, { AxiosHeaders } from 'axios';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CADExports } from '../CAD';

jest.mock('~/hooks', () => ({ useLocalize: () => (key: string) => key }));

describe('CAD export dialog', () => {
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  function response(data: unknown) {
    return {
      data,
      status: 200,
      statusText: 'OK',
      headers: {},
      config: { headers: new AxiosHeaders() },
    };
  }
  afterEach(() => {
    jest.restoreAllMocks();
    URL.createObjectURL = originalCreate;
    URL.revokeObjectURL = originalRevoke;
  });
  function show() {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <CADExports enabled />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'com_ui_cad_exports' }));
  }
  test('shows an empty workspace and refresh action', async () => {
    const get = jest.spyOn(axios, 'get').mockResolvedValue(response([]));
    show();
    expect(await screen.findByText('com_ui_cad_empty')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'com_ui_cad_refresh' }));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });
  test('lists exports and downloads through the authenticated data service', async () => {
    const get = jest
      .spyOn(axios, 'get')
      .mockResolvedValueOnce(
        response([{ filename: 'part.step', bytes: 12, updatedAt: '2026-10-13T00:00:00Z' }]),
      )
      .mockResolvedValueOnce(response(new Blob(['geometry'])));
    URL.createObjectURL = jest.fn(() => 'blob:cad-export');
    URL.revokeObjectURL = jest.fn();
    jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    show();
    expect(await screen.findByText('part.step')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'com_ui_download' }));
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith('/api/cad/part.step', { responseType: 'blob' }),
    );
  });
  test('shows actionable failure copy', async () => {
    jest.spyOn(axios, 'get').mockRejectedValue(new Error('Network failure'));
    show();
    expect(await screen.findByRole('alert')).toHaveTextContent('com_ui_cad_load_error');
  });
});
