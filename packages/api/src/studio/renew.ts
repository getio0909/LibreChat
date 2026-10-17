import { logger } from '@librechat/data-schemas';
import { createSafeUser } from '~/utils/env';

type RenewRequest = {
  token: string;
  body: Record<string, unknown>;
  user: Parameters<typeof createSafeUser>[0];
  tenantId?: string | null;
  mcpUrl?: string;
  sharedToken?: string;
  fetchImpl?: typeof fetch;
};

type RenewResponse = { status: number; body: Record<string, unknown> };

const RENEW_FIELDS = [
  'resource_uri',
  'image_id',
  'model_id',
  'editor_url',
  'preview_url',
  'before_url',
  'after_url',
  'model_url',
] as const;

export async function renewStudioLinks(input: RenewRequest): Promise<RenewResponse> {
  const principal = createSafeUser(input.user, input.tenantId).studioPrincipal;
  if (!principal) {
    return { status: 401, body: { error: 'Unauthenticated' } };
  }
  if (!/^[A-Za-z0-9_-]{1,512}\.[a-f0-9]{64}$/.test(input.token)) {
    return { status: 400, body: { error: 'Invalid Studio token' } };
  }
  if (!input.mcpUrl || !input.sharedToken) {
    logger.error('[StudioRenew] Studio connection is not configured');
    return { status: 503, body: { error: 'Studio renewal is unavailable' } };
  }

  let endpoint: URL;
  try {
    const mcpEndpoint = new URL(input.mcpUrl);
    if (!['http:', 'https:'].includes(mcpEndpoint.protocol) || mcpEndpoint.pathname !== '/mcp') {
      throw new Error('Invalid Studio MCP URL');
    }
    endpoint = new URL(`/files/${encodeURIComponent(input.token)}`, mcpEndpoint);
  } catch {
    logger.error('[StudioRenew] Studio connection URL is invalid');
    return { status: 503, body: { error: 'Studio renewal is unavailable' } };
  }

  const body: Record<string, unknown> = { action: 'renew' };
  for (const field of RENEW_FIELDS) {
    const value = input.body?.[field];
    if (typeof value === 'string' && value.length <= 2048) {
      body[field] = value;
    }
  }
  try {
    const response = await (input.fetchImpl ?? fetch)(endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Studio-Token': input.sharedToken,
        'X-LibreChat-User-ID': principal,
      },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      return {
        status: [403, 404, 409].includes(response.status) ? response.status : 503,
        body: { error: 'Studio renewal failed' },
      };
    }
    const data: unknown = await response.json();
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      throw new Error('Invalid Studio renewal response');
    }
    return { status: 200, body: data as Record<string, unknown> };
  } catch {
    logger.warn('[StudioRenew] Studio renewal request failed');
    return { status: 503, body: { error: 'Studio renewal is unavailable' } };
  }
}
