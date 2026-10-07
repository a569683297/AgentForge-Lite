import { Space, Tag, Typography } from 'antd';
import type { ReactNode } from 'react';
import styles from '../styles/shell.module.css';

interface PageHeaderProps {
  title: string;
  description: string;
  action?: ReactNode;
  badge?: string;
}

export function PageHeader({ title, description, action, badge }: PageHeaderProps) {
  return (
    <div className={styles.pageHeader}>
      <div>
        <Typography.Title level={1}>{title}</Typography.Title>
        <Typography.Paragraph>{description}</Typography.Paragraph>
      </div>
      <Space size={8} align="center">
        {badge ? <Tag color="warning">{badge}</Tag> : null}
        {action}
      </Space>
    </div>
  );
}
