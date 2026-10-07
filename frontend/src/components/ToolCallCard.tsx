import { CaretRightOutlined, CheckCircleOutlined, CloseCircleOutlined, QuestionCircleOutlined, ToolOutlined } from '@ant-design/icons';
import { Collapse, Tag } from 'antd';
import type { PairedToolCall } from '../lib/format';
import { StatusDot } from './StatusDot';
import styles from '../styles/chat.module.css';

/** 实时对话（`POST /api/chat`）只返回工具名，不返回入参与结果 —— 如实说"没有"，不编造一条像结论的文案。 */
const UNKNOWN_ARGS = '本轮接口未返回入参';
const UNKNOWN_RESULT = '本轮接口未返回返回摘要（/api/chat 只返回工具名）';

export function ToolCallCard({ tool, source }: {
  tool: PairedToolCall;
  /** 工具来源（server 名 / `local`）。查不到时传 `undefined` —— 卡片显示「来源未知」，不默认成 `local`。 */
  source?: string;
}) {
  const known = tool.result != null;
  const tone = !known ? 'muted' : tool.failed ? 'danger' : 'success';
  const argsText = tool.arguments == null ? null : JSON.stringify(tool.arguments, null, 2);
  return (
    <div className={styles.toolCall}>
      <Collapse ghost expandIcon={({ isActive }) => <CaretRightOutlined rotate={isActive ? 90 : 0} />} items={[{
        key: tool.name,
        label: (
          <div className={styles.toolSummary}>
            <StatusDot tone={tone} />
            <ToolOutlined />
            <span className="af-mono">{tool.name}</span>
            <Tag>{source ?? '来源未知'}</Tag>
            <span className={styles.toolResult}>
              {!known ? <QuestionCircleOutlined /> : tool.failed ? <CloseCircleOutlined /> : <CheckCircleOutlined />}
            </span>
          </div>
        ),
        children: (
          <div className={styles.toolDetail}>
            <span>入参</span>
            {argsText ? <code>{argsText}</code> : <span className={styles.toolMissing}>{UNKNOWN_ARGS}</span>}
            <span>返回摘要</span>
            {tool.result ? <code>{tool.result}</code> : <span className={styles.toolMissing}>{UNKNOWN_RESULT}</span>}
          </div>
        ),
      }]} />
    </div>
  );
}
