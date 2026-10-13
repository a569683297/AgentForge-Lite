import { CaretRightOutlined, CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined, QuestionCircleOutlined, ToolOutlined } from '@ant-design/icons';
import { Collapse, Tag } from 'antd';
import type { PairedToolCall } from '../lib/format';
import { StatusDot } from './StatusDot';
import styles from '../styles/chat.module.css';

/** 两个实时接口（/api/chat 与流式 /api/chat/stream）都只回传工具名（流式另带执行状态），
 *  不返回入参与结果 —— 如实说"没有"，不编造一条像结论的文案。 */
const UNKNOWN_ARGS = '本轮接口未返回入参';
const UNKNOWN_RESULT = '本轮接口未返回返回摘要';

export function ToolCallCard({ tool, source }: {
  tool: PairedToolCall;
  /** 工具来源（server 名 / `local`）。查不到时传 `undefined` —— 卡片显示「来源未知」，不默认成 `local`。 */
  source?: string;
}) {
  // 「运行中」与「结果未知」刻意分开渲染：前者是"正在发生"（转圈），
  // 后者是"发生过但数据源没给"（问号）—— 都画问号会把两种状态混成同一件事
  const running = tool.running === true;
  const known = tool.result != null;
  const tone = running ? 'info' : !known ? 'muted' : tool.failed ? 'danger' : 'success';
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
              {running ? <LoadingOutlined spin /> : !known ? <QuestionCircleOutlined /> : tool.failed ? <CloseCircleOutlined /> : <CheckCircleOutlined />}
            </span>
          </div>
        ),
        children: running ? (
          <div className={styles.toolDetail}>
            <span className={styles.toolMissing}>工具执行中…</span>
          </div>
        ) : (
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
