import styles from '../styles/shell.module.css';

export type StatusTone = 'success' | 'warning' | 'danger' | 'info' | 'muted';

export function StatusDot({ tone = 'muted', label }: { tone?: StatusTone; label?: string }) {
  return (
    <span className={styles.status}>
      <span className={`${styles.statusDot} ${styles[`status_${tone}`]}`} aria-hidden="true" />
      {label ? <span>{label}</span> : null}
    </span>
  );
}
