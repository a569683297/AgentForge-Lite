import { ReloadOutlined, SafetyCertificateOutlined, TeamOutlined } from '@ant-design/icons';
import { Button, Card, Input, Select, Space, Switch, Table, Tabs, Tag, Tooltip, message } from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { listAudit, listAuditActions, listUsers, patchUser, type AuditItem } from '../api/admin';
import { ApiError } from '../api/client';
import { EmptyState, ErrorState } from '../components/EmptyState';
import { PageHeader } from '../components/PageHeader';
import { TableSkeleton } from '../components/Skeleton';
import { getCurrentUser, type AuthUser } from '../lib/auth';
import { formatDateTime } from '../lib/format';
import styles from '../styles/dataPages.module.css';

/**
 * 用户管理 + 审计日志（仅管理员可见）
 * ===================================
 * 两个 Tab 放在同一页，因为它们回答的是同一个问题：**"这个系统最近发生了什么"**。
 * 分成两页会让"谁把某人禁用了"和"那次禁用留下的痕迹"隔开，排查时要来回跳。
 *
 * 三个前端的克制（都是为了不制造假信息）：
 *  ① 操作失败时**不用乐观更新**：先请求、成功了再改本地那一行。
 *     乐观更新一旦失败，界面会显示一个根本没发生的状态 —— 而权限类操作尤其不能这样。
 *  ② 后端拒绝（400）的原因原样弹出（"不能禁用当前登录的账号"），不自己编文案。
 *  ③ 这里**不做角色判断**（除了藏入口）：能不能改，由后端说了算。
 *     前端再判断一遍只会出现"界面允许但后端拒绝"的不一致，反而更难理解。
 */

const ROLE_LABEL: Record<string, string> = { admin: '管理员', user: '普通用户' };

export function AdminPage() {
  const me = getCurrentUser();
  const [tab, setTab] = useState<'users' | 'audit'>('users');

  // ---- 用户 ----
  const [users, setUsers] = useState<AuthUser[]>([]);
  const [userTotal, setUserTotal] = useState(0);
  const [keyword, setKeyword] = useState('');
  const [usersLoading, setUsersLoading] = useState(true);
  const [usersError, setUsersError] = useState<string | null>(null);
  const [savingId, setSavingId] = useState<string | null>(null);

  const loadUsers = useCallback(async () => {
    setUsersError(null);
    try {
      const page = await listUsers({ limit: 200, keyword });
      setUsers(page.items);
      setUserTotal(page.total);
    } catch (reason: unknown) {
      setUsersError(explain(reason, '无法加载用户列表'));
    } finally {
      setUsersLoading(false);
    }
  }, [keyword]);

  useEffect(() => { void loadUsers(); }, [loadUsers]);

  async function applyPatch(user: AuthUser, patch: { is_active?: boolean; role?: 'user' | 'admin' }) {
    setSavingId(user.id);
    try {
      const updated = await patchUser(user.id, patch);
      // 用后端返回的对象替换本地那一行（而不是自己拼）：
      // 后端的更新可能带默认值/级联效果，本地拼容易与真实状态漂移
      setUsers((rows) => rows.map((row) => (row.id === updated.id ? { ...row, ...updated } : row)));
      message.success('已更新');
    } catch (reason: unknown) {
      message.error(explain(reason, '更新失败'));
    } finally {
      setSavingId(null);
    }
  }

  // ---- 审计 ----
  const [audit, setAudit] = useState<AuditItem[]>([]);
  const [auditTotal, setAuditTotal] = useState(0);
  const [actions, setActions] = useState<string[]>([]);
  const [actionPrefix, setActionPrefix] = useState<string>('');
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);

  const loadAudit = useCallback(async () => {
    setAuditLoading(true);
    setAuditError(null);
    try {
      const page = await listAudit({ limit: 200, action: actionPrefix || undefined });
      setAudit(page.items);
      setAuditTotal(page.total);
    } catch (reason: unknown) {
      setAuditError(explain(reason, '无法加载审计日志'));
    } finally {
      setAuditLoading(false);
    }
  }, [actionPrefix]);

  useEffect(() => {
    if (tab !== 'audit') return;
    void loadAudit();
  }, [tab, loadAudit]);

  useEffect(() => { void listAuditActions().then(setActions).catch(() => undefined); }, []);

  // 动作前缀（auth. / admin. / document. / tool_server.）——
  // 用前缀而不是全量清单：清单会随版本增长，下拉里塞 20 个值没人愿意看
  const prefixes = useMemo(() => {
    const set = new Set<string>();
    actions.forEach((action) => {
      const head = action.split('.')[0];
      if (head) set.add(`${head}.`);
    });
    return [...set].sort();
  }, [actions]);

  return (
    <>
      <PageHeader
        title="用户与审计"
        description="账号状态、角色变更，以及每一次状态变更留下的痕迹"
      />

      <Tabs
        activeKey={tab}
        onChange={(key) => setTab(key as 'users' | 'audit')}
        items={[
          {
            key: 'users',
            label: <span><TeamOutlined /> 用户（{userTotal}）</span>,
            children: (
              <Card
                size="small"
                title={`共 ${userTotal} 个账号`}
                extra={(
                  <Space>
                    <Input.Search
                      allowClear
                      placeholder="搜邮箱或显示名"
                      style={{ width: 200 }}
                      onSearch={(value) => setKeyword(value.trim())}
                    />
                    <Button icon={<ReloadOutlined />} onClick={() => void loadUsers()}>刷新</Button>
                  </Space>
                )}
              >
                {usersLoading ? <TableSkeleton rows={6} /> : null}
                {usersError ? <ErrorState message={usersError} action="重试" onAction={() => void loadUsers()} /> : null}
                {!usersLoading && !usersError ? (
                  <Table<AuthUser>
                    size="small"
                    rowKey="id"
                    dataSource={users}
                    pagination={false}
                    locale={{ emptyText: <EmptyState message="没有匹配的账号" /> }}
                    columns={[
                      {
                        title: '邮箱',
                        dataIndex: 'email',
                        render: (email: string, row) => (
                          <Space size={6}>
                            <span>{email}</span>
                            {row.id === me?.id ? <Tag bordered={false} color="gold">你</Tag> : null}
                          </Space>
                        ),
                      },
                      { title: '显示名', dataIndex: 'display_name' },
                      {
                        title: '角色',
                        dataIndex: 'role',
                        width: 130,
                        render: (role: string, row) => (
                          <Select
                            size="small"
                            value={role}
                            style={{ width: 110 }}
                            disabled={savingId === row.id}
                            // 降级最后一个管理员会被后端拒绝（400）——
                            // 这里不提前禁用：让人看到"为什么不行"比"按钮点不动"更有用
                            onChange={(value) => void applyPatch(row, { role: value as 'user' | 'admin' })}
                            options={[
                              { value: 'user', label: ROLE_LABEL.user },
                              { value: 'admin', label: ROLE_LABEL.admin },
                            ]}
                          />
                        ),
                      },
                      {
                        title: '状态',
                        dataIndex: 'is_active',
                        width: 110,
                        render: (active: boolean, row) => (
                          <Tooltip title={active ? '点击禁用（立即无法登录）' : '点击启用'}>
                            <Switch
                              size="small"
                              checked={active}
                              loading={savingId === row.id}
                              checkedChildren="启用"
                              unCheckedChildren="禁用"
                              onChange={(checked) => void applyPatch(row, { is_active: checked })}
                            />
                          </Tooltip>
                        ),
                      },
                      {
                        title: '最后登录',
                        dataIndex: 'last_login_at',
                        width: 160,
                        render: (value: string | null) => (value ? formatDateTime(value) : '从未登录'),
                      },
                      {
                        title: '注册时间',
                        dataIndex: 'created_at',
                        width: 160,
                        render: (value: string) => formatDateTime(value),
                      },
                    ]}
                  />
                ) : null}
              </Card>
            ),
          },
          {
            key: 'audit',
            label: <span><SafetyCertificateOutlined /> 审计日志（{auditTotal}）</span>,
            children: (
              <Card
                size="small"
                title={`最近 ${audit.length} 条 / 共 ${auditTotal} 条`}
                extra={(
                  <Space>
                    <Select
                      allowClear
                      size="small"
                      placeholder="按动作过滤"
                      style={{ width: 160 }}
                      value={actionPrefix || undefined}
                      onChange={(value) => setActionPrefix(value ?? '')}
                      options={prefixes.map((prefix) => ({ value: prefix, label: prefix }))}
                    />
                    <Button size="small" icon={<ReloadOutlined />} onClick={() => void loadAudit()}>刷新</Button>
                  </Space>
                )}
              >
                {auditLoading ? <TableSkeleton rows={6} /> : null}
                {auditError ? <ErrorState message={auditError} action="重试" onAction={() => void loadAudit()} /> : null}
                {!auditLoading && !auditError ? (
                  <Table<AuditItem>
                    size="small"
                    rowKey="id"
                    dataSource={audit}
                    pagination={false}
                    locale={{ emptyText: <EmptyState message="还没有审计记录：登录、改密、禁用账号等动作会出现在这里" /> }}
                    columns={[
                      { title: '时间', dataIndex: 'created_at', width: 160, render: (value: string) => formatDateTime(value) },
                      { title: '动作', dataIndex: 'action', width: 200, render: (value: string) => <Tag bordered={false}>{value}</Tag> },
                      { title: '操作者', dataIndex: 'actor_email', width: 220, render: (value: string) => value || '—' },
                      {
                        title: '对象',
                        width: 220,
                        render: (_, row) => (row.target_type ? `${row.target_type} · ${row.target_id.slice(0, 8)}…` : '—'),
                      },
                      {
                        title: '详情',
                        render: (_, row) => (
                          <span className={styles.inlineMeta}>
                            {row.detail ? JSON.stringify(row.detail) : '—'}
                          </span>
                        ),
                      },
                    ]}
                  />
                ) : null}
              </Card>
            ),
          },
        ]}
      />
    </>
  );
}

/** 把异常翻成一句人话（403 单独说清楚，别让人以为服务坏了）。 */
function explain(reason: unknown, fallback: string): string {
  if (reason instanceof ApiError) {
    if (reason.status === 403) return '需要管理员权限';
    return reason.message;
  }
  return fallback;
}
