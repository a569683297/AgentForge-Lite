import { Card } from 'antd';
import type { ReactNode } from 'react';
import styles from '../styles/shell.module.css';

export function MetricCard({ label, value, suffix, note, prefix }: {
  label: string;
  value: ReactNode;
  suffix?: ReactNode;
  note?: ReactNode;
  prefix?: ReactNode;
}) {
  return (
    <Card className={styles.metricCard} size="small">
      <div className={styles.metricLabel}>{label}</div>
      <div className={styles.metricValue}>{prefix}{value}{suffix ? <span className={styles.metricSuffix}>{suffix}</span> : null}</div>
      {note ? <div className={styles.metricNote}>{note}</div> : null}
    </Card>
  );
}
