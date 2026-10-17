import { renewStudioLinks } from './renew';

const token = `${'a'.repeat(40)}.${'b'.repeat(64)}`;
const base = {
  token,
  body: { action: 'renew', image_id: 'image-1', owner: 'forged-owner' },
  mcpUrl: 'http://studio:8000/mcp',
  sharedToken: 'shared-secret',
  getAdmittedServers: async () => ({ zyb_studio: { url: 'http://studio:8000/mcp' } }),
};

test('renewal forwards only authenticated tenant scoped owner identity', async () => {
  const fetchImpl = Object.assign(
    jest.fn().mockImplementation(
      async () =>
        new Response(JSON.stringify({ status: 'renewed', preview_url: '/studio-files/new' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    ),
    { preconnect: jest.fn() },
  );
  const user = { id: '65f0000000000000000000a1' };
  const tenantA = await renewStudioLinks({ ...base, user, tenantId: 'tenant-a', fetchImpl });
  const tenantB = await renewStudioLinks({ ...base, user, tenantId: 'tenant-b', fetchImpl });

  expect(tenantA.status).toBe(200);
  expect(tenantB.status).toBe(200);
  const firstInit = fetchImpl.mock.calls[0][1] as RequestInit;
  const secondInit = fetchImpl.mock.calls[1][1] as RequestInit;
  const firstHeaders = firstInit.headers as Record<string, string>;
  const secondHeaders = secondInit.headers as Record<string, string>;
  expect(firstHeaders['X-LibreChat-User-ID']).toMatch(/^[a-f0-9]{24}$/);
  expect(firstHeaders['X-LibreChat-User-ID']).not.toBe(secondHeaders['X-LibreChat-User-ID']);
  expect(JSON.parse(firstInit.body as string)).toEqual({ action: 'renew', image_id: 'image-1' });
  expect(fetchImpl.mock.calls[0][0].href).toBe(`http://studio:8000/files/${token}`);
});

test('renewal fails closed for an unauthenticated user and a denied Studio owner', async () => {
  const fetchImpl = Object.assign(
    jest.fn().mockResolvedValue(new Response('{}', { status: 403 })),
    { preconnect: jest.fn() },
  );
  expect((await renewStudioLinks({ ...base, user: null, fetchImpl })).status).toBe(401);
  expect(fetchImpl).not.toHaveBeenCalled();
  expect((await renewStudioLinks({ ...base, user: { id: 'user-a' }, fetchImpl })).status).toBe(403);
});

test('renewal stops before proxying when Studio is removed from current MCP admission', async () => {
  const fetchImpl = Object.assign(jest.fn(), { preconnect: jest.fn() });
  const user = { id: '65f0000000000000000000a1' };
  expect(
    (
      await renewStudioLinks({
        ...base,
        user,
        fetchImpl,
        getAdmittedServers: async () => ({ another_server: {} }),
      })
    ).status,
  ).toBe(403);
  expect(
    (
      await renewStudioLinks({
        ...base,
        user,
        fetchImpl,
        getAdmittedServers: async () => ({ zyb_studio: undefined }),
      })
    ).status,
  ).toBe(403);
  expect(
    (
      await renewStudioLinks({
        ...base,
        user,
        fetchImpl,
        getAdmittedServers: async () => {
          throw new Error('database unavailable');
        },
      })
    ).status,
  ).toBe(503);
  expect(fetchImpl).not.toHaveBeenCalled();
});
