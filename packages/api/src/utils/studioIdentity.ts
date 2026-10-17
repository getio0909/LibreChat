import crypto from 'node:crypto';

// 跨租户复用同一用户编号时，为工作台目录和 MCP 会话生成不泄露租户编号的独立身份
export function deriveStudioStorageUserId(userId: string, tenantId?: string | null): string {
  if (!tenantId) {
    return userId;
  }
  return crypto
    .createHash('sha256')
    .update(JSON.stringify(['studio-user-v1', tenantId, userId]))
    .digest('hex')
    .slice(0, 24);
}
