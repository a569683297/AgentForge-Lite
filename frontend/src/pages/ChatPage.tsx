import { PaperClipOutlined, PlusOutlined, SendOutlined } from '@ant-design/icons';
import { Button, Input, Space } from 'antd';
import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { sendMessageStream } from '../api/chat';
import { getSessionMessages } from '../api/sessions';
import { getTools } from '../api/tools';
import { pairToolMessages, type PairedToolCall } from '../lib/format';
import type { MessageItem, SourceItem } from '../types/api';
import { CitationDrawer } from '../components/CitationDrawer';
import { ErrorState } from '../components/EmptyState';
import { MessageList, type ChatMessageView } from '../components/MessageList';
import styles from '../styles/chat.module.css';

/**
 * 把 PG 全量消息映射成气泡列表。
 *
 * ⚠ 配对纪律（修过的 bug，2026-10-13 实测暴露）：
 *   一条 assistant 消息**只配它自己那一段工具消息**（紧跟其后的连续 tool 消息）。
 *   原实现把 `history.slice(index + 1)` 整段丢给 `pairToolMessages`，而该函数会遍历
 *   数组里**所有**带 tool_calls 的 assistant —— 后续轮次的工具被重复配对到前面的消息上：
 *   8 次真实调用（4 轮 × 2 并行）刷新后渲染成 30 张卡片。多轮下钻的会话必现。
 */
function fromHistory(history: MessageItem[]): ChatMessageView[] {
  return history
    .map((message, index) => ({ message, index }))
    .filter(({ message }) => message.role !== 'tool')
    .map(({ message, index }): ChatMessageView => ({
      id: `${message.created_at}-${index}`,
      role: message.role === 'user' ? 'user' : 'assistant',
      content: message.content,
      createdAt: message.created_at,
      tools: message.role === 'assistant' && message.tool_calls ? pairToolMessages([message, ...consecutiveToolMessages(history, index)]) : undefined,
    }));
}

/** 取紧跟在 index 之后的一段**连续** tool 消息（一条 assistant 的本轮返回值都在这一段里） */
function consecutiveToolMessages(history: MessageItem[], index: number): MessageItem[] {
  const run: MessageItem[] = [];
  for (let i = index + 1; i < history.length && history[i].role === 'tool'; i += 1) {
    run.push(history[i]);
  }
  return run;
}

export function ChatPage({ onSessionsRefresh }: { onSessionsRefresh: () => void }) {
  // 会话 id 直接从 URL 读（/chat/:sessionId）；/chat（新会话）时是 undefined。
  // 不再用 useState 镜像它 —— URL 是唯一真源：
  // 刷新 /chat/xxx 时 useParams 拿到的还是同一个 xxx，历史会话自然恢复。
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
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

  // 首个 token / 工具事件到达后，气泡自身就是"正在工作"的反馈 —— 收起通用加载提示，
  // 避免同一件事两个提示（发送按钮的 spinner 保留：它同时兼任"阻止重复发送"）。
  const streamHasOutput = messages.some(
    (item) => item.streaming && (item.content.length > 0 || (item.tools?.length ?? 0) > 0),
  );

  async function submit() {
    const message = input.trim();
    if (!message || loading) return;
    setError(null);
    setInput('');

    // 一条"占位气泡"随流生长：token 到了就往它身上追加 ——
    // 流式体验的核心：不再等整轮结束才出现一条完整回答
    const assistantId = `assistant-${Date.now()}`;
    setMessages((current) => [
      ...current,
      { id: `user-${Date.now()}`, role: 'user', content: message, createdAt: new Date().toISOString() },
      { id: assistantId, role: 'assistant', content: '', createdAt: new Date().toISOString(), streaming: true, toolSources },
    ]);
    setLoading(true);

    // 流式累加态（闭包变量）：onEvent 是**同步**回调，token/reset/tool 按到达顺序生效。
    // 不放 state 的原因：真正的展示状态在 messages 里（每次打补丁），
    // 这两个变量只是"当前进度"的累加器，没必要触发额外渲染。
    let answer = '';
    let tools: PairedToolCall[] = [];
    const patchAssistant = (patch: Partial<ChatMessageView>) => {
      setMessages((current) => current.map((item) => (item.id === assistantId ? { ...item, ...patch } : item)));
    };

    // 「收到 done 或 error」才算正常收尾；两个都没有 = 连接异常中断（如进程被杀）
    let settled = false;

    try {
      await sendMessageStream(message, sessionId, (event) => {
        switch (event.type) {
          case 'token':
            answer += event.text;
            patchAssistant({ content: answer });
            break;
          case 'reset':
            // 模型中途说了话又改去调工具 —— 那段文本不属于最终回答，清掉
            // （后端只在"确实有中间文本被发出"时才发这一帧；见 llm_gateway 的 reset 说明）
            answer = '';
            patchAssistant({ content: '' });
            break;
          case 'tool': {
            if (event.phase === 'start') {
              tools = [...tools, { name: event.name, arguments: null, result: null, failed: false, running: true }];
            } else {
              // 配对该工具**最早的**运行中实例：同名工具可以在一轮里被调用多次，
              // 后端 start/end 是成对有序发出的，这里按序配对即可
              const index = tools.findIndex((tool) => tool.name === event.name && tool.running);
              if (index >= 0) {
                tools = tools.map((tool, i) => (i === index ? { ...tool, running: false, failed: event.ok === false } : tool));
              }
            }
            patchAssistant({ tools });
            break;
          }
          case 'done':
            settled = true;
            setSources(event.sources);
            setInvalidCitations(event.invalid_citations);
            // 以 done.answer 收口对齐：正常情况与累加值相同；reset 边界 / 丢帧时，
            // 这一步保证"屏幕上的最终文本 == 落库文本"
            patchAssistant({ content: event.answer });
            // 首条消息会产生新会话 id：把地址栏从 /chat 换成 /chat/:id。
            // **刻意等 done 才跳**（不在 meta 帧就跳）：navigate 会触发路由参数变化 →
            // 历史加载 effect 触发 setMessages(history) —— 流式期间跳会把正在生长的
            // 气泡冲掉；done 之后跳，PG 里已有本轮完整记录，重读出来就是对的。
            // 判断 id 变化才跳：多轮对话时 URL 没变，避免无意义的历史写入。
            if (event.session_id !== sessionId) {
              navigate(`/chat/${event.session_id}`, { replace: true });
            }
            // 侧栏「最近会话」需要包含这条新会话（列表数据在 App 手里）
            onSessionsRefresh();
            break;
          case 'error':
            settled = true;
            setError(event.message);
            break;
          case 'meta':
            // 暂不消费：会话 id 以 done 为准（理由见上面 navigate 的注释）
            break;
        }
      });
      if (!settled) {
        setError('流式连接意外中断 —— 本轮回答可能不完整');
      }
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '对话请求失败，请检查 API 服务');
    } finally {
      setLoading(false);
      // 无论正常结束还是异常结束，都摘掉流式标记：气泡切回引用感知渲染、残留态归零
      patchAssistant({ streaming: false });
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  function resetConversation() {
    // 回 /chat = 「新会话」的地址（push —— 可以用后退回到刚才那个会话）。
    // 已在 /chat 时无需跳转。状态清空照旧（effect 会在 sessionId 变化时再兜一次底）。
    if (sessionId) navigate('/chat');
    setMessages([]);
    setSources([]);
    setInvalidCitations([]);
    setError(null);
  }

  return (
    <>
      <div className={styles.chatPage}>
        <div className={styles.chatToolbar}>
          <div className={styles.chatToolbarTitle}><strong>{sessionId ? '当前会话' : '新会话'}</strong><span className="af-mono af-muted">{sessionId ? sessionId.slice(0, 12) : '未创建'}</span></div>
          <Space size={8}><Button size="small" icon={<PlusOutlined />} onClick={resetConversation}>新建会话</Button><Button size="small">导出</Button></Space>
        </div>
        <div className={styles.messageList} ref={listRef}>
          <MessageList messages={messages} sources={sources} invalidCitations={invalidCitations} loading={loading && !streamHasOutput} onCitation={setSelectedSource} toolSources={toolSources} />
          {error ? <ErrorState message={error} action="重试" onAction={() => setError(null)} /> : null}
          {!messages.length && !loading && !error ? <div className={styles.chatEmpty}>输入一个问题，观察 Agent 的检索和工具调用过程。</div> : null}
        </div>
        <div className={styles.composer}>
          <div className={styles.composerTools}><Button size="small" type="text" icon={<PaperClipOutlined />} disabled aria-label="添加附件" /><span className={styles.composerHint}>Enter 发送 · Shift+Enter 换行</span></div>
          <Input.TextArea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={handleKeyDown} autoSize={{ minRows: 2, maxRows: 5 }} placeholder="问点什么…" />
          <div className={styles.composerBottom}><span className={styles.composerContext}>上下文：知识库 · 当前会话</span><Button type="primary" icon={<SendOutlined />} onClick={() => void submit()} loading={loading}>发送</Button></div>
        </div>
      </div>
      <CitationDrawer source={selectedSource} onClose={() => setSelectedSource(null)} onOpenKnowledge={() => { setSelectedSource(null); navigate('/knowledge'); }} />
    </>
  );
}
