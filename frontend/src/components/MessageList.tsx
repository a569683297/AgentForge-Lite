import { Alert } from 'antd';
import type { ReactNode } from 'react';
import type { SourceItem } from '../types/api';
import { extractCitationParts, formatDateTime } from '../lib/format';
import { ToolCallCard } from './ToolCallCard';
import styles from '../styles/chat.module.css';
import type { PairedToolCall } from '../lib/format';

export interface ChatMessageView {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  createdAt?: string;
  tools?: PairedToolCall[];
  toolSources?: Record<string, string>;
  /**
   * 流式进行中的气泡（M6 优化②）：为 true 时内容按**纯文本**渲染。
   *
   * 为什么要跳过引用解析：引用来源表（sources）在流结束后才随 done 事件到达，
   * 流式期间直接跑 renderAnswer 会把每个 [n] 都判成"无此来源"（红块闪烁）——
   * 那是**还没到**，不是**不存在**。流结束（或从 PG 重读）后自然恢复引用渲染。
   */
  streaming?: boolean;
}

function renderAnswer(answer: string, sources: SourceItem[], invalid: number[], onCitation: (source: SourceItem) => void): ReactNode {
  const sourceIndexes = sources.map((source) => source.index);
  return extractCitationParts(answer, sourceIndexes).map((part, index) => {
    if (part.type === 'text') return <span key={`${part.type}-${index}`}>{part.value}</span>;
    const source = sources.find((candidate) => candidate.index === Number(part.value));
    const isInvalid = invalid.includes(Number(part.value)) || !part.valid;
    return isInvalid || !source
      ? <span className={styles.invalidCitation} title="无此来源" key={`${part.type}-${index}`}>[{part.value}]</span>
      : <button className={styles.citationLink} onClick={() => onCitation(source)} key={`${part.type}-${index}`}>[{part.value}]</button>;
  });
}

export function MessageList({ messages, sources, invalidCitations, loading, onCitation, toolSources }: {
  messages: ChatMessageView[];
  sources: SourceItem[];
  invalidCitations: number[];
  loading?: boolean;
  onCitation: (source: SourceItem) => void;
  /**
   * 全局工具来源表（name → source），作为**兜底**：
   * 从 PG 重读的历史消息没带来源快照（toolSources 只在实时对话时随消息快照），
   * 用它补上 —— 否则同样的工具卡片，实时看是「local」、刷新后变「来源未知」。
   * 消息自带的 `message.toolSources` 优先（它代表"当时"的来源，可能与现在不同）。
   */
  toolSources?: Record<string, string>;
}) {
  return (
    <div className={styles.messageList}>
      {messages.map((message) => (
        <article className={`${styles.message} ${message.role === 'user' ? styles.userMessage : styles.agentMessage}`} key={message.id}>
          <div className={styles.messageLabel}>{message.role === 'user' ? '你' : 'Agent'}{message.role === 'assistant' ? <span className="af-mono af-muted">runtime</span> : null}</div>
          {message.tools?.map((tool, index) => <ToolCallCard key={`${message.id}-tool-${index}`} tool={tool} source={message.toolSources?.[tool.name] ?? toolSources?.[tool.name]} />)}
          {message.content ? (
            <div className={styles.messageBody}>
              {/* 流式气泡按纯文本渲染（理由见 ChatMessageView.streaming 的注释）；
                  流结束后恢复引用感知渲染 */}
              {message.role === 'assistant' && !message.streaming
                ? renderAnswer(message.content, sources, invalidCitations, onCitation)
                : message.content}
            </div>
          ) : null}
          {message.createdAt ? <div className={styles.messageMeta}>{formatDateTime(message.createdAt)}</div> : null}
        </article>
      ))}
      {loading ? <div className={styles.thinking}><Alert type="info" showIcon message="Agent 正在处理请求…" /></div> : null}
    </div>
  );
}
