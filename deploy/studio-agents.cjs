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
        instructions: preset.promptPrefix ?? '',
        tools: [...STUDIO_AGENT_TOOLS],
        mcpServerNames: ['zyb_studio'],
        model_parameters: modelParameters,
      },
    };
  });
}

function isStudioOwnedAgent(agent, expectedName) {
  if (!agent || typeof agent !== 'object') {
    return false;
  }
  if (typeof agent.id !== 'string' || !/^agent_[A-Za-z0-9_-]+$/.test(agent.id)) {
    return false;
  }
  if (expectedName !== undefined && agent.name !== expectedName) {
    return false;
  }
  if (!Array.isArray(agent.tools)) {
    return false;
  }
  return STUDIO_AGENT_TOOLS.every((requiredTool) => agent.tools.includes(requiredTool));
}

function findStudioOwnedAgent(agents, expectedName, persistedId) {
  if (!Array.isArray(agents)) {
    return null;
  }
  if (typeof persistedId === 'string' && persistedId.length > 0) {
    const byId = agents.find(
      (candidate) => candidate && candidate.id === persistedId && isStudioOwnedAgent(candidate),
    );
    if (byId) {
      return byId;
    }
  }
  return (
    agents.find((candidate) => isStudioOwnedAgent(candidate, expectedName)) ?? null
  );
}

async function collectPaginatedAgents(fetchPage, options = {}) {
  const limit = options.limit ?? 100;
  const maxPages = options.maxPages ?? 50;
  const collected = [];
  const seenCursors = new Set();
  let cursor = null;

  for (let pageIndex = 0; pageIndex < maxPages; pageIndex++) {
    const page = await fetchPage({ limit, cursor });
    const items = Array.isArray(page?.data) ? page.data : [];
    collected.push(...items);
    const nextCursor = typeof page?.after === 'string' && page.after.length > 0 ? page.after : null;
    if (!page?.has_more || !nextCursor || seenCursors.has(nextCursor)) {
      break;
    }
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  }

  return collected;
}

module.exports = {
  STUDIO_AGENT_PLACEHOLDERS,
  STUDIO_AGENT_PROVIDERS,
  STUDIO_AGENT_TOOLS,
  buildStudioAgentDefinitions,
  isStudioOwnedAgent,
  findStudioOwnedAgent,
  collectPaginatedAgents,
};
