import { PaperClipOutlined, PlusOutlined, SendOutlined } from '@ant-design/icons';
import { Button, Input, Space } from 'antd';
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { sendMessage } from '../api/chat';
import { getSessionMessages } from '../api/sessions';
import { getTools } from '../api/tools';
import { pairToolMessages, type PairedToolCall } from '../lib/format';
import type { MessageItem, SourceItem } from '../types/api';
import { CitationDrawer } from '../components/CitationDrawer';
import { ErrorState } from '../components/EmptyState';
import { MessageList, type ChatMessageView } from '../components/MessageList';
import styles from '../styles/chat.module.css';

function fromHistory(history: MessageItem[]): ChatMessageView[] {
  return history.filter((message) => message.role !== 'tool').map((message, index) => ({
    id: `${message.created_at}-${index}`,
    role: message.role === 'user' ? 'user' : 'assistant',
    content: message.content,
    createdAt: message.created_at,
    tools: message.role === 'assistant' && message.tool_calls ? pairToolMessages([message, ...history.slice(index + 1)]) : undefined,
  }));
}

export function ChatPage({ sessionId, onNewSession, onSessionChange, onOpenKnowledge }: { sessionId?: string; onNewSession: () => void; onSessionChange: (id: string) => void; onOpenKnowledge: () => void }) {
  const [currentSessionId, setCurrentSessionId] = useState(sessionId);
  const [messages, setMessages] = useState<ChatMessageView[]>([]);
  const [sources, setSources] = useState<SourceItem[]>([]);
  const [invalidCitations, setInvalidCitations] = useState<number[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedSource, setSelectedSource] = useState<SourceItem | null>(null);
  const [toolSources, setToolSources] = useState<Record<string, string>>({});
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setCurrentSessionId(sessionId);
    if (!sessionId) {
      setMessages([]);
      setSources([]);
      setInvalidCitations([]);
      return;
    }
    void getSessionMessages(sessionId).then((history) => setMessages(fromHistory(history))).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '无法加载会话历史'));
  }, [sessionId]);

  useEffect(() => {
    void getTools().then((response) => setToolSources(Object.fromEntries(response.tools.map((tool) => [tool.name, tool.source])))).catch(() => undefined);
  }, []);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, loading]);

  // POST /api/chat 只回工具名（tool_calls: string[]），不返回入参与结果 → 一律落 null，
  // 由 ToolCallCard 显式渲染「接口未返回」；这里**不编造**入参对象与结果文案。
  const toolCallFactory = useMemo(() => (names: string[]): PairedToolCall[] => names.map((name) => ({ name, arguments: null, result: null, failed: false })), []);

  async function submit() {
    const message = input.trim();
    if (!message || loading) return;
    setError(null);
    setInput('');
    setMessages((current) => [...current, { id: `user-${Date.now()}`, role: 'user', content: message, createdAt: new Date().toISOString() }]);
    setLoading(true);
    try {
      const response = await sendMessage(message, currentSessionId);
      setCurrentSessionId(response.session_id);
      onSessionChange(response.session_id);
      setSources(response.sources);
      setInvalidCitations(response.invalid_citations);
      setMessages((current) => [...current, {
        id: `assistant-${Date.now()}`,
        role: 'assistant',
        content: response.answer,
        createdAt: new Date().toISOString(),
        tools: toolCallFactory(response.tool_calls),
        toolSources,
      }]);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '对话请求失败，请检查 API 服务');
    } finally {
      setLoading(false);
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  function resetConversation() {
    setCurrentSessionId(undefined);
    setMessages([]);
    setSources([]);
    setInvalidCitations([]);
    setError(null);
    onNewSession();
  }

  return (
    <>
      <div className={styles.chatPage}>
        <div className={styles.chatToolbar}>
          <div className={styles.chatToolbarTitle}><strong>{currentSessionId ? '当前会话' : '新会话'}</strong><span className="af-mono af-muted">{currentSessionId ? currentSessionId.slice(0, 12) : '未创建'}</span></div>
          <Space size={8}><Button size="small" icon={<PlusOutlined />} onClick={resetConversation}>新建会话</Button><Button size="small">导出</Button></Space>
        </div>
        <div className={styles.messageList} ref={listRef}>
          <MessageList messages={messages} sources={sources} invalidCitations={invalidCitations} loading={loading} onCitation={setSelectedSource} />
          {error ? <ErrorState message={error} action="重试" onAction={() => setError(null)} /> : null}
          {!messages.length && !loading && !error ? <div className={styles.chatEmpty}>输入一个问题，观察 Agent 的检索和工具调用过程。</div> : null}
        </div>
        <div className={styles.composer}>
          <div className={styles.composerTools}><Button size="small" type="text" icon={<PaperClipOutlined />} disabled aria-label="添加附件" /><span className={styles.composerHint}>Enter 发送 · Shift+Enter 换行</span></div>
          <Input.TextArea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={handleKeyDown} autoSize={{ minRows: 2, maxRows: 5 }} placeholder="问点什么…" />
          <div className={styles.composerBottom}><span className={styles.composerContext}>上下文：知识库 · 当前会话</span><Button type="primary" icon={<SendOutlined />} onClick={() => void submit()} loading={loading}>发送</Button></div>
        </div>
      </div>
      <CitationDrawer source={selectedSource} onClose={() => setSelectedSource(null)} onOpenKnowledge={() => { setSelectedSource(null); onOpenKnowledge(); }} />
    </>
  );
}
