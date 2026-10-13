import { once } from 'node:events';
import { createServer } from 'node:http';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import type { Server } from 'node:http';
import { createImageMCPHandler } from './image';

describe('image MCP provider boundary', () => {
  const request = jest.fn<ReturnType<typeof fetch>, Parameters<typeof fetch>>();
  const reportError = jest.fn();
  const clients: Client[] = [];
  let server: Server;
  let baseURL: string;

  beforeEach(async () => {
    request.mockReset();
    reportError.mockClear();
    server = createServer(createImageMCPHandler({ request, reportError }));
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    const address = server.address();
    if (!address || typeof address === 'string') {
      throw new Error('The test server did not bind a TCP port.');
    }
    baseURL = `http://127.0.0.1:${address.port}`;
  });

  afterEach(async () => {
    await Promise.all(clients.splice(0).map((client) => client.close()));
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  });

  async function connect(provider: string, key?: string) {
    const client = new Client({ name: 'image-test', version: '1.0.0' });
    clients.push(client);
    await client.connect(
      new StreamableHTTPClientTransport(new URL(`${baseURL}/${provider}`), {
        requestInit: {
          headers: {
            'x-image-model':
              provider === 'seedream'
                ? 'doubao-seedream-5-0-flash-260915'
                : 'gemini-3.1-flash-image',
            ...(key ? { 'x-provider-api-key': key } : {}),
          },
        },
      }),
    );
    return client;
  }

  test('discovers tools without a provider key and gives a safe configuration error', async () => {
    const client = await connect('seedream');
    expect(await client.listTools()).toMatchObject({ tools: [{ name: 'generate_image' }] });
    expect(
      await client.callTool({
        name: 'generate_image',
        arguments: { prompt: 'A temple courtyard' },
      }),
    ).toMatchObject({ isError: true });
    expect(request).not.toHaveBeenCalled();
  });

  test('returns a Seedream image using the configured model and request-scoped key', async () => {
    request.mockResolvedValue(new Response(JSON.stringify({ data: [{ b64_json: 'aW1hZ2U=' }] })));
    const client = await connect('seedream', 'seedream-key');
    expect(
      await client.callTool({
        name: 'generate_image',
        arguments: { prompt: 'A temple courtyard', size: '2K' },
      }),
    ).toMatchObject({
      content: [{ type: 'image', mimeType: 'image/png', data: 'aW1hZ2U=' }],
    });
    expect(request).toHaveBeenCalledWith(
      'https://ark.cn-beijing.volces.com/api/v3/images/generations',
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer seedream-key' }),
        body: expect.stringContaining('doubao-seedream-5-0-flash-260915'),
      }),
    );
  });

  test('returns Gemini image content and keeps concurrent users credentials separate', async () => {
    request.mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            candidates: [
              { content: { parts: [{ inlineData: { mimeType: 'image/png', data: 'aW1hZ2U=' } }] } },
            ],
          }),
        ),
    );
    const [first, second] = await Promise.all([
      connect('gemini', 'first-user-key'),
      connect('gemini', 'second-user-key'),
    ]);
    const results = await Promise.all(
      [first, second].map((client) =>
        client.callTool({ name: 'generate_image', arguments: { prompt: 'A temple courtyard' } }),
      ),
    );
    for (const result of results)
      expect(result).toMatchObject({ content: [{ type: 'image', mimeType: 'image/png' }] });
    expect(request).toHaveBeenCalledWith(
      expect.stringContaining('gemini-3.1-flash-image:generateContent'),
      expect.objectContaining({
        headers: expect.objectContaining({ 'x-goog-api-key': 'first-user-key' }),
      }),
    );
    expect(request).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        headers: expect.objectContaining({ 'x-goog-api-key': 'second-user-key' }),
      }),
    );
  });

  test.each(['rejected', 'network', 'invalid'])(
    'does not disclose provider diagnostics on %s failure',
    async (mode) => {
      if (mode === 'rejected')
        request.mockResolvedValue(new Response('private-key-and-prompt', { status: 401 }));
      if (mode === 'network') request.mockRejectedValue(new Error('private-key-and-prompt'));
      if (mode === 'invalid')
        request.mockResolvedValue(
          new Response(JSON.stringify({ diagnostic: 'private-key-and-prompt' })),
        );
      const client = await connect('seedream', 'private-key-and-prompt');
      const result = await client.callTool({
        name: 'generate_image',
        arguments: { prompt: 'private-key-and-prompt' },
      });
      expect(result).toMatchObject({ isError: true });
      expect(JSON.stringify(result)).not.toContain('private-key-and-prompt');
      expect(JSON.stringify(reportError.mock.calls)).not.toContain('private-key-and-prompt');
    },
  );
});
