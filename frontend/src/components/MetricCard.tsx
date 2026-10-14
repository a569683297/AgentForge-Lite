import { InfoCircleOutlined } from '@ant-design/icons';
import { Card, Tooltip } from 'antd';
import type { ReactNode } from 'react';
import styles from '../styles/shell.module.css';

export function MetricCard({ label, value, suffix, note, prefix, labelTip }: {
  label: string;
  value: ReactNode;
  suffix?: ReactNode;
  note?: ReactNode;
  prefix?: ReactNode;
  /** 标题旁的 info 悬浮说明（与运行卡片同款）；不传则不显示图标（如分析页的示例卡） */
  labelTip?: ReactNode;
}) {
  return (
    <Card className={styles.metricCard} size="small">
      <div className={styles.metricLabel}>
        {label}
        {labelTip ? <Tooltip title={labelTip}><span className={styles.metricLabelInfo}><InfoCircleOutlined /></span></Tooltip> : null}
      </div>
      <div className={styles.metricValue}>{prefix}{value}{suffix ? <span className={styles.metricSuffix}>{suffix}</span> : null}</div>
      {note ? <div className={styles.metricNote}>{note}</div> : null}
    </Card>
  );
}
