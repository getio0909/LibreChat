'use strict';

// 构建两个预置 Studio 智能体（主创设计与快速设计）的持久化配置定义
const STUDIO_AGENT_PLACEHOLDERS = Object.freeze({
  'zyb-design': 'agent___STUDIO_AGENT_ID_MAIN__',
  'zyb-design-fast': 'agent___STUDIO_AGENT_ID_FAST__',
});

const STUDIO_AGENT_PROVIDERS = Object.freeze({
  'zyb-design': 'google',
  'zyb-design-fast': 'OpenRouter',
});

const STUDIO_AGENT_TOOLS = Object.freeze(['gemini_image_gen', 'sys__all__sys_mcp_zyb_studio']);
const STUDIO_AGENT_SUPPORT_CONTACT = Object.freeze({
  name: '庄严宝刹',
  email: '',
});
const STUDIO_AGENT_OWNER_MARKERS = Object.freeze({
  'zyb-design': '<!-- librechat-studio-owner:zyb-design:v1 -->',
  'zyb-design-fast': '<!-- librechat-studio-owner:zyb-design-fast:v1 -->',
});

function buildStudioAgentDefinitions(config) {
  const specs = config?.modelSpecs?.list ?? [];
  return specs.map((spec) => {
    const placeholder = STUDIO_AGENT_PLACEHOLDERS[spec.name];
    const provider = STUDIO_AGENT_PROVIDERS[spec.name];
    if (!placeholder || !provider) {
      throw new Error(`Unknown Studio modelSpec: ${spec?.name}`);
    }
    const preset = spec.preset ?? {};
    const modelParameters = {};
    for (const key of [
      'thinking',
      'thinkingLevel',
      'maxContextTokens',
      'maxOutputTokens',
      'max_tokens',
    ]) {
      if (preset[key] !== undefined) {
        modelParameters[key] = preset[key];
      }
    }
    return {
      specName: spec.name,
      placeholder,
      agent: {
        id: placeholder,
        name: spec.label,
        description: spec.description,
        provider,
        model: preset.model,
        instructions: `${preset.promptPrefix ?? ''}\n\n${STUDIO_AGENT_OWNER_MARKERS[spec.name]}`,
        tools: [...STUDIO_AGENT_TOOLS],
        mcpServerNames: ['zyb_studio'],
        support_contact: { ...STUDIO_AGENT_SUPPORT_CONTACT },
        model_parameters: modelParameters,
      },
    };
  });
}

function isStudioOwnedAgent(agent, specName) {
  if (!agent || typeof agent !== 'object') {
    return false;
  }
  if (typeof agent.id !== 'string' || !/^agent_[A-Za-z0-9_-]+$/.test(agent.id)) {
    return false;
  }
  const marker = STUDIO_AGENT_OWNER_MARKERS[specName];
  if (!marker || typeof agent.instructions !== 'string') {
    return false;
  }
  return agent.instructions.endsWith(marker);
}

function findStudioOwnedAgent(agents, specName, persistedId) {
  if (!Array.isArray(agents)) {
    return null;
  }
  if (typeof persistedId === 'string' && persistedId.length > 0) {
    const byId = agents.find((candidate) => candidate && candidate.id === persistedId);
    if (byId) {
      if (!isStudioOwnedAgent(byId, specName)) {
        throw new Error('Persisted Studio agent ID has no matching ownership marker');
      }
      return byId;
    }
  }
  const matches = agents.filter((candidate) => isStudioOwnedAgent(candidate, specName));
  if (matches.length > 1) {
    throw new Error('Multiple Studio agents carry the same ownership marker');
  }
  return matches[0] ?? null;
}

async function collectPaginatedAgents(fetchPage, options = {}) {
  const limit = options.limit ?? 100;
  const maxPages = options.maxPages ?? 50;
  const collected = [];
  const seenCursors = new Set();
  let cursor = null;

  for (let pageIndex = 0; pageIndex < maxPages; pageIndex++) {
    const page = await fetchPage({ limit, cursor });
    if (!Array.isArray(page?.data) || typeof page.has_more !== 'boolean') {
      throw new Error('Invalid Studio agent listing page');
    }
    const items = page.data;
    collected.push(...items);
    const nextCursor = typeof page?.after === 'string' && page.after.length > 0 ? page.after : null;
    if (!page.has_more) {
      return collected;
    }
    if (!nextCursor || seenCursors.has(nextCursor)) {
      throw new Error('Incomplete Studio agent pagination');
    }
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  }

  throw new Error('Studio agent listing exceeded pagination limit');
}

module.exports = {
  STUDIO_AGENT_PLACEHOLDERS,
  STUDIO_AGENT_PROVIDERS,
  STUDIO_AGENT_TOOLS,
  STUDIO_AGENT_SUPPORT_CONTACT,
  STUDIO_AGENT_OWNER_MARKERS,
  buildStudioAgentDefinitions,
  isStudioOwnedAgent,
  findStudioOwnedAgent,
  collectPaginatedAgents,
};
