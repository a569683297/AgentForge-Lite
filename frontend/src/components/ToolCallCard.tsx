import { CaretRightOutlined, CheckCircleOutlined, CloseCircleOutlined, ToolOutlined } from '@ant-design/icons';
import { Collapse, Tag } from 'antd';
import type { PairedToolCall } from '../lib/format';
import { StatusDot } from './StatusDot';
import styles from '../styles/chat.module.css';

export function ToolCallCard({ tool, source = 'local' }: { tool: PairedToolCall; source?: string }) {
  const failed = tool.failed;
  return (
    <div className={styles.toolCall}>
      <Collapse ghost expandIcon={({ isActive }) => <CaretRightOutlined rotate={isActive ? 90 : 0} />} items={[{
        key: tool.name,
        label: (
          <div className={styles.toolSummary}>
            <StatusDot tone={failed ? 'danger' : 'success'} />
            <ToolOutlined />
            <span className="af-mono">{tool.name}</span>
            <Tag>{source}</Tag>
            <span className={styles.toolResult}>{failed ? <CloseCircleOutlined /> : <CheckCircleOutlined />}</span>
          </div>
        ),
        children: (
          <div className={styles.toolDetail}>
            <span>入参</span><code>{JSON.stringify(tool.arguments, null, 2)}</code>
            <span>返回摘要</span><code>{tool.result}</code>
          </div>
        ),
      }]} />
    </div>
  );
}
