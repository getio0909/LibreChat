import React from 'react';
import { RecoilRoot } from 'recoil';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { ChatSettingsContext, defaultChatSettings } from '~/Providers/ChatSettingsContext';
import Landing from '../Landing';

let mockStartupConfig: { appTitle?: string; interface: { customWelcome?: string } } = {
  interface: {},
};
let mockConversation: Record<string, unknown> | null = null;
let mockAgentsMap: Record<string, any> | undefined;
let mockAssistantMap: Record<string, any> | undefined;

jest.mock('@react-spring/web', () => ({
  easings: {
    easeOutCubic: jest.fn(),
  },
}));

jest.mock('librechat-data-provider', () => ({
  EModelEndpoint: {
    azureOpenAI: 'azureOpenAI',
    openAI: 'openAI',
  },
}));

jest.mock('@librechat/client', () => ({
  BirthdayIcon: () => <span data-testid="birthday-icon" />,
  TooltipAnchor: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
  SplitText: ({ text }: { text: string }) => <span>{text}</span>,
  useRemScale: () => 1,
}));

jest.mock('~/Providers', () => ({
  useChatContext: () => ({ conversation: mockConversation }),
  useAgentsMapContext: () => mockAgentsMap,
  useAssistantsMapContext: () => mockAssistantMap,
}));

jest.mock('~/data-provider', () => ({
  useGetStartupConfig: () => ({ data: mockStartupConfig }),
  useGetEndpointsQuery: () => ({ data: {} }),
}));

jest.mock('~/hooks', () => ({
  useAuthContext: () => ({ user: undefined }),
  useGreeting: () => 'Welcome',
  useLocalize: () => (key: string, options?: { 0: string }) => {
    const translations: Record<string, string> = {
      com_ui_logo: `${options?.[0]} Logo`,
      com_agents_contact: 'Contact',
      com_agents_no_contact_available: 'No contact available',
      com_ui_temporary: 'Temporary Chat',
      com_ui_temporary_description:
        "This chat won't appear in your history and will be deleted automatically.",
    };
    return translations[key] || key;
  },
}));

jest.mock('~/utils', () => ({
  CONFIG_HTML_MEDIA_ATTR: {},
  CONFIG_HTML_MEDIA_TAGS: [],
  cn: (...classes: string[]) => classes.filter(Boolean).join(' '),
  createConfigHtmlSanitizer: () => (html: string) => html,
  getIconEndpoint: ({ endpoint }: { endpoint: string }) => endpoint,
  getModelSpec: () => undefined,
  getEntity: ({
    endpoint,
    agentsMap,
    assistantMap,
    agent_id,
    assistant_id,
  }: {
    endpoint: string;
    agentsMap?: Record<string, any>;
    assistantMap?: Record<string, any>;
    agent_id?: string;
    assistant_id?: string;
  }) => {
    if (endpoint === 'agents' && agent_id != null) {
      return { entity: agentsMap?.[agent_id], isAgent: true, isAssistant: false };
    }
    if (assistant_id != null) {
      return { entity: assistantMap?.[assistant_id], isAgent: false, isAssistant: true };
    }
    return { entity: undefined, isAgent: false, isAssistant: false };
  },
}));

jest.mock('~/components/Endpoints/ConvoIcon', () => () => <span data-testid="convo-icon" />);

function renderLanding({
  isTemporary = false,
  centerFormOnLanding = false,
}: {
  isTemporary?: boolean;
  centerFormOnLanding?: boolean;
} = {}) {
  return render(
    <RecoilRoot>
      <ChatSettingsContext.Provider value={{ ...defaultChatSettings, isTemporary }}>
        <Landing centerFormOnLanding={centerFormOnLanding} />
      </ChatSettingsContext.Provider>
    </RecoilRoot>,
  );
}

describe('Landing agent contact', () => {
  beforeEach(() => {
    mockConversation = null;
    mockAgentsMap = undefined;
    mockAssistantMap = undefined;
  });

  it('shows contact for the selected agent from agentsMap', () => {
    mockConversation = {
      endpoint: 'agents',
      agent_id: 'agent-1',
    };
    mockAgentsMap = {
      'agent-1': {
        id: 'agent-1',
        name: 'Portal Remote Agent',
        description: 'Remote Agent Showcase',
        owner_contact: { name: 'Owner User' },
      },
    };

    renderLanding();

    expect(screen.getByText('Portal Remote Agent')).toBeInTheDocument();
    expect(screen.getByText('Remote Agent Showcase')).toBeInTheDocument();
    expect(screen.getByText('Contact:')).toBeInTheDocument();
    expect(screen.getByText('Owner User')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Owner User' })).not.toBeInTheDocument();
  });

  it('renders the email link without inline baseline space beside the contact label', () => {
    mockConversation = {
      endpoint: 'agents',
      agent_id: 'agent-1',
    };
    mockAgentsMap = {
      'agent-1': {
        id: 'agent-1',
        name: 'LibreChat',
        description: 'Contact Danny if you see any errors',
        support_contact: { email: 'messagedaniel@pm.me' },
      },
    };

    renderLanding();

    const link = screen.getByRole('link', { name: 'messagedaniel@pm.me' });
    expect(link).toHaveAttribute('href', 'mailto:messagedaniel@pm.me');
    expect(link).toHaveClass('block');
    expect(link).not.toHaveClass('inline-block');
    expect(screen.getByText('Contact:').parentElement).toHaveClass('items-center');
  });

  it('does not show contact when the selected agent is missing from agentsMap', () => {
    mockConversation = {
      endpoint: 'agents',
      agent_id: 'missing-agent',
      greeting: 'Start chatting',
    };
    mockAgentsMap = {};

    renderLanding();

    expect(screen.queryByText('Contact:')).not.toBeInTheDocument();
    expect(screen.queryByText('No contact available')).not.toBeInTheDocument();
  });

  it('does not show contact for assistants', () => {
    mockConversation = {
      endpoint: 'assistants',
      assistant_id: 'assistant-1',
    };
    mockAssistantMap = {
      'assistant-1': {
        id: 'assistant-1',
        name: 'Assistant',
        description: 'Assistant description',
      },
    };

    renderLanding();

    expect(screen.getByText('Assistant')).toBeInTheDocument();
    expect(screen.queryByText('Contact:')).not.toBeInTheDocument();
  });
});

describe('Landing temporary chat empty state', () => {
  beforeEach(() => {
    mockConversation = null;
    mockAgentsMap = undefined;
    mockAssistantMap = undefined;
  });

  it('replaces the greeting with the temporary chat explanation', () => {
    renderLanding({ isTemporary: true });

    expect(screen.getByText('Temporary Chat')).toBeInTheDocument();
    expect(
      screen.getByText("This chat won't appear in your history and will be deleted automatically."),
    ).toBeInTheDocument();
    expect(screen.queryByText('Welcome')).not.toBeInTheDocument();
    expect(screen.queryByTestId('convo-icon')).not.toBeInTheDocument();
  });

  it('hides the agent identity and contact while temporary', () => {
    mockConversation = {
      endpoint: 'agents',
      agent_id: 'agent-1',
    };
    mockAgentsMap = {
      'agent-1': {
        id: 'agent-1',
        name: 'Portal Remote Agent',
        description: 'Remote Agent Showcase',
        owner_contact: { name: 'Owner User' },
      },
    };

    renderLanding({ isTemporary: true });

    expect(screen.getByText('Temporary Chat')).toBeInTheDocument();
    expect(screen.queryByText('Portal Remote Agent')).not.toBeInTheDocument();
    expect(screen.queryByText('Remote Agent Showcase')).not.toBeInTheDocument();
    expect(screen.queryByText('Contact:')).not.toBeInTheDocument();
  });

  it('keeps the normal greeting when temporary chat is off', () => {
    renderLanding();

    expect(screen.getByText('Welcome')).toBeInTheDocument();
    expect(screen.queryByText('Temporary Chat')).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: '庄严宝刹 Logo' })).toHaveAttribute(
      'src',
      'assets/zybc-seal.svg',
    );
  });
});

describe('Landing brand identity', () => {
  beforeEach(() => {
    mockConversation = { endpoint: 'openAI', model: 'gpt-6' };
    mockAgentsMap = undefined;
    mockAssistantMap = undefined;
    mockStartupConfig = {
      appTitle: '庄严宝刹 AI 设计助手',
      interface: { customWelcome: '庄严宝刹 AI 设计助手' },
    };
  });

  afterEach(() => {
    mockStartupConfig = { interface: {} };
  });

  it.each([true, false])('uses the brand seal with centered composer=%s', (centerFormOnLanding) => {
    renderLanding({ centerFormOnLanding });

    const logo = screen.getByRole('img', { name: '庄严宝刹 AI 设计助手 Logo' });
    expect(logo).toHaveAttribute('src', 'assets/zybc-seal.svg');
    expect(logo).toHaveAttribute('width', '40');
    expect(logo).toHaveAttribute('height', '40');
    expect(logo).toHaveClass('object-contain');
    expect(screen.getByText('庄严宝刹 AI 设计助手')).toBeInTheDocument();
    expect(screen.queryByTestId('convo-icon')).not.toBeInTheDocument();
  });

  it.each(['anthropic', 'custom'])('does not substitute a provider icon for %s', (endpoint) => {
    mockConversation = { endpoint, model: 'another-model' };
    renderLanding();

    expect(screen.getByRole('img')).toHaveAttribute('src', 'assets/zybc-seal.svg');
    expect(screen.queryByTestId('convo-icon')).not.toBeInTheDocument();
  });

  it('preserves the temporary-chat indicator instead of displaying the brand seal', () => {
    renderLanding({ isTemporary: true });

    expect(screen.getByText('Temporary Chat')).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
});
