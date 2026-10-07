import { Button, Empty } from 'antd';
import type { ReactNode } from 'react';
import styles from '../styles/shell.module.css';

export function EmptyState({ message, action, onAction }: { message: string; action?: string; onAction?: () => void }) {
  return (
    <div className={styles.emptyState}>
      <Empty image={null} description={message} />
      {action && onAction ? <Button type="link" onClick={onAction}>{action} →</Button> : null}
    </div>
  );
}

export function ErrorState({ message, action, onAction }: { message: string; action?: string; onAction?: () => void }) {
  return (
    <div className={styles.errorState} role="alert">
      <span>{message}</span>
      {action && onAction ? <Button type="link" onClick={onAction}>{action} →</Button> : null}
    </div>
  );
}

export function InlineMeta({ children }: { children: ReactNode }) {
  return <span className={styles.inlineMeta}>{children}</span>;
}
