import { Alert } from 'antd';
import type { ReactNode } from 'react';
import type { SourceItem } from '../types/api';
import { extractCitationParts } from '../lib/format';
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

export function MessageList({ messages, sources, invalidCitations, loading, onCitation }: {
  messages: ChatMessageView[];
  sources: SourceItem[];
  invalidCitations: number[];
  loading?: boolean;
  onCitation: (source: SourceItem) => void;
}) {
  return (
    <div className={styles.messageList}>
      {messages.map((message) => (
        <article className={`${styles.message} ${message.role === 'user' ? styles.userMessage : styles.agentMessage}`} key={message.id}>
          <div className={styles.messageLabel}>{message.role === 'user' ? '你' : 'Agent'}{message.role === 'assistant' ? <span className="af-mono af-muted">runtime</span> : null}</div>
          {message.tools?.map((tool, index) => <ToolCallCard key={`${message.id}-tool-${index}`} tool={tool} source={message.toolSources?.[tool.name]} />)}
          {message.content ? <div className={styles.messageBody}>{message.role === 'assistant' ? renderAnswer(message.content, sources, invalidCitations, onCitation) : message.content}</div> : null}
          {message.createdAt ? <div className={styles.messageMeta}>{message.createdAt}</div> : null}
        </article>
      ))}
      {loading ? <div className={styles.thinking}><Alert type="info" showIcon message="Agent 正在处理请求…" /></div> : null}
    </div>
  );
}
