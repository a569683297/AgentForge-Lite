import { Skeleton as AntSkeleton } from 'antd';
import styles from '../styles/shell.module.css';

export function TableSkeleton({ rows = 5, columns = 5 }: { rows?: number; columns?: number }) {
  return (
    <div className={styles.tableSkeleton} aria-label="正在加载">
      {Array.from({ length: rows }, (_, row) => (
        <div className={styles.skeletonRow} key={row}>
          {Array.from({ length: columns }, (_, column) => (
            <AntSkeleton.Input active size="small" block key={column} />
          ))}
        </div>
      ))}
    </div>
  );
}
