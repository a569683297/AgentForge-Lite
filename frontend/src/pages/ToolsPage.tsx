import {
  ApiOutlined,
  DeleteOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SettingOutlined,
  ToolOutlined,
} from '@ant-design/icons';
import { Button, Card, Input, Modal, Popconfirm, Select, Space, Tag, Tooltip, message } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  deleteToolServer,
  getToolServers,
  getTools,
  reconnectToolServer,
  setToolServerStatus,
  testTool,
  uploadToolServer,
} from '../api/tools';
import { EmptyState, ErrorState } from '../components/EmptyState';
import { PageHeader } from '../components/PageHeader';
import { StatusDot } from '../components/StatusDot';
import type { StatusTone } from '../components/StatusDot';
import { TableSkeleton } from '../components/Skeleton';
import type { ToolItem, ToolServerOut, ToolsResponse } from '../types/api';
import styles from '../styles/dataPages.module.css';

/**
 * 把文件名"猜"成一个合法的工具名（小写字母开头、只含小写字母/数字/下划线、≥3 位）。
 *
 * 为什么要猜：这个名字同时是**工具名前缀**，而用户在不知道这层含义时，
 * 最自然的写法恰好是不合法的（`demo-tools` / `Demo Tools`）。
 * 预填一个合法值，比让他对着 400 改三遍强。预填只是建议，最终以后端校验为准。
 */
export function guessToolName(filename: string): string {
  const base = filename.replace(/\.py$/i, '').toLowerCase();
  let slug = base.replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '');
  if (!/^[a-z]/.test(slug)) slug = `tool_${slug}`;
  return slug.slice(0, 32).padEnd(3, '0');
}

export function ToolsPage() {
  const [data, setData] = useState<ToolsResponse | null>(null);
  const [servers, setServers] = useState<ToolServerOut[]>([]);
  const [query, setQuery] = useState('');
  const [source, setSource] = useState('all');
  const [expanded, setExpanded] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // ---- 上传 ----
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [description, setDescription] = useState('');
  const [uploading, setUploading] = useState(false);

  // ---- 试跑 ----
  const [probe, setProbe] = useState<{
    server: ToolServerOut;
    tool: string;
    args: string;
    output: string;
    running: boolean;
  } | null>(null);

  const reload = useCallback(async () => {
    try {
      const [tools, list] = await Promise.all([getTools(), getToolServers()]);
      setData(tools);
      setServers(list.servers);
      setError(null);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '无法加载工具注册表');
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const serverConnected = useMemo(
    () => new Map((data?.servers ?? []).map((server) => [server.name, server.connected])),
    [data],
  );
  const sources = useMemo(
    () =>
      Object.entries(data?.by_source ?? {}).map(([key, names]) => ({
        name: key,
        count: names.length,
      })),
    [data],
  );
  const filtered = useMemo(
    () =>
      (data?.tools ?? []).filter((tool) => {
        const matchesQuery = `${tool.name} ${tool.description}`
          .toLowerCase()
          .includes(query.toLowerCase());
        return matchesQuery && (source === 'all' || tool.source === source);
      }),
    [data, query, source],
  );

  function toolStatus(tool: ToolItem) {
    const connected = tool.source === 'local' || serverConnected.get(tool.source) !== false;
    return connected
      ? { tone: 'success' as const, label: '可用' }
      : { tone: 'warning' as const, label: '未连接' };
  }

  /**
   * 一台上传的 server 该显示成什么。
   *
   * `active + 未连接` 单独一档：它不是"停用"，而是"该跑却没跑" ——
   * 这是唯一需要用户动手的状态（点重连），所以文案必须点出来。
   */
  function serverState(server: ToolServerOut): { tone: StatusTone; label: string } {
    if (server.status === 'disabled') return { tone: 'muted', label: '已停用' };
    if (server.status === 'failed') return { tone: 'danger', label: '启动失败' };
    if (!server.connected) return { tone: 'warning', label: '该跑但没连上' };
    return { tone: 'success', label: '运行中' };
  }

  async function run(action: () => Promise<unknown>, okText: string) {
    try {
      await action();
      message.success(okText);
      await reload();
    } catch (reason: unknown) {
      message.error(reason instanceof Error ? reason.message : '操作失败');
    }
  }

  async function handleUpload() {
    if (!file) {
      message.warning('请先选择 .py 文件');
      return;
    }
    setUploading(true);
    try {
      const result = await uploadToolServer({
        file,
        name,
        displayName: displayName || undefined,
        description: description || undefined,
      });
      if (result.connected) {
        message.success(`已接入，发现 ${result.tool_count} 个工具`);
      } else {
        // 202 的真实语义：记录建好了，但没连上。不能报"成功"。
        message.warning(`已保存，但没连上：${result.last_error ?? '未知原因'}`);
      }
      setFile(null);
      setName('');
      setDisplayName('');
      setDescription('');
      setUploadOpen(false);
      await reload();
    } catch (reason: unknown) {
      message.error(reason instanceof Error ? reason.message : '上传失败');
    } finally {
      setUploading(false);
    }
  }

  async function handleProbe() {
    if (!probe) return;
    let args: Record<string, unknown>;
    try {
      args = probe.args.trim() ? (JSON.parse(probe.args) as Record<string, unknown>) : {};
    } catch {
      message.error('参数必须是合法 JSON，例如 {"text": "你好"}');
      return;
    }
    setProbe({ ...probe, running: true, output: '' });
    try {
      const result = await testTool(probe.server.id, probe.tool, args);
      setProbe({ ...probe, running: false, output: result.output });
    } catch (reason: unknown) {
      setProbe({
        ...probe,
        running: false,
        output: reason instanceof Error ? reason.message : '调用失败',
      });
    }
  }

  return (
    <div>
      <PageHeader
        title="工具注册表"
        description={`已注册的 MCP 与本地工具，共 ${data?.total ?? '—'} 个。`}
        action={
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setUploadOpen(true)}>
            注册工具
          </Button>
        }
      />

      {/* ---- 我上传的工具 ---- */}
      <div className={styles.sectionHead}>
        <div>
          <div className={styles.sectionTitle}>我上传的工具</div>
          <div className={styles.sectionDescription}>
            上传一个 MCP Server 程序（.py），它跑在独立沙箱容器里；工具名会自动加
            <code> {`<名字>_`} </code>
            前缀，避免与其他 server 撞名。
          </div>
        </div>
        <div className={styles.sectionHeadRight}>
          <Button size="small" icon={<ReloadOutlined />} onClick={() => void reload()}>
            刷新
          </Button>
        </div>
      </div>

      {!servers.length ? (
        <EmptyState
          message="还没有上传过工具。"
          action="注册第一个工具"
          onAction={() => setUploadOpen(true)}
        />
      ) : (
        <div className={styles.toolGrid}>
          {servers.map((server) => {
            const state = serverState(server);
            return (
              <Card className={styles.toolCard} styles={{ body: { padding: 0 } }} key={server.id}>
                <div className={styles.toolCardHead}>
                  <span className={styles.toolIcon}>
                    <ApiOutlined />
                  </span>
                  <StatusDot tone={state.tone} label={state.label} />
                </div>
                <div className={styles.toolCardName}>{server.display_name}</div>
                <div className={styles.toolCardDescription}>
                  {server.description || `工具名前缀：${server.name}_`}
                </div>
                {server.last_error ? (
                  <div className={styles.errorText} style={{ padding: '0 16px 8px' }}>
                    {server.last_error}
                  </div>
                ) : null}
                <div className={styles.toolCardFoot}>
                  <Space size={4} wrap>
                    <Tag>{server.tool_count} 个工具</Tag>
                    <Tag
                      color={
                        server.status === 'active'
                          ? 'green'
                          : server.status === 'failed'
                            ? 'red'
                            : 'default'
                      }
                    >
                      {server.status}
                    </Tag>
                  </Space>
                  <Space size={0}>
                    <Tooltip title="试跑一个工具">
                      <Button
                        type="text"
                        size="small"
                        icon={<PlayCircleOutlined />}
                        disabled={!server.connected}
                        onClick={() =>
                          setProbe({
                            server,
                            tool: server.tools[0] ?? '',
                            args: '{}',
                            output: '',
                            running: false,
                          })
                        }
                      />
                    </Tooltip>
                    <Tooltip title="重连">
                      <Button
                        type="text"
                        size="small"
                        icon={<ReloadOutlined />}
                        onClick={() => void run(() => reconnectToolServer(server.id), '已重连')}
                      />
                    </Tooltip>
                    <Tooltip title={server.status === 'disabled' ? '启用' : '停用'}>
                      <Button
                        type="text"
                        size="small"
                        icon={<SettingOutlined />}
                        onClick={() =>
                          void run(
                            () =>
                              setToolServerStatus(
                                server.id,
                                server.status === 'disabled' ? 'active' : 'disabled',
                              ),
                            server.status === 'disabled' ? '已启用' : '已停用',
                          )
                        }
                      />
                    </Tooltip>
                    <Popconfirm
                      title="删除这个工具 server？"
                      description="会断开连接、摘掉它的工具并删除代码，不可恢复。"
                      okText="删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                      onConfirm={() => void run(() => deleteToolServer(server.id), '已删除')}
                    >
                      <Button type="text" size="small" danger icon={<DeleteOutlined />} />
                    </Popconfirm>
                  </Space>
                </div>
              </Card>
            );
          })}
        </div>
      )}

      {/* ---- 全部工具 ---- */}
      <div className={styles.sectionHead} style={{ marginTop: 24 }}>
        <div>
          <div className={styles.sectionTitle}>全部工具</div>
          <div className={styles.sectionDescription}>
            内置工具、MCP server 与上传工具都在这张表里 —— 两条注册路径，最终合流到同一个注册表。
          </div>
        </div>
      </div>

      {error ? (
        <ErrorState message={error} action="重新加载" onAction={() => void reload()} />
      ) : !data ? (
        <TableSkeleton rows={5} columns={3} />
      ) : !data.tools.length ? (
        <EmptyState message="未连接 Server，当前没有可展示的工具。" />
      ) : (
        <>
          <div className={styles.toolFilters}>
            <Input
              allowClear
              placeholder="搜索工具名称或描述"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className={styles.searchBox}
            />
            <button
              className={`${styles.filterButton} ${source === 'all' ? styles.filterButtonActive : ''}`}
              onClick={() => setSource('all')}
            >
              全部 ({data.total})
            </button>
            {sources.map((item) => (
              <button
                className={`${styles.filterButton} ${source === item.name ? styles.filterButtonActive : ''}`}
                onClick={() => setSource(item.name)}
                key={item.name}
              >
                {item.name} ({item.count})
              </button>
            ))}
          </div>
          {!filtered.length ? (
            <EmptyState
              message="没有匹配的工具。"
              action="清空筛选"
              onAction={() => {
                setQuery('');
                setSource('all');
              }}
            />
          ) : (
            <div className={styles.toolGrid}>
              {filtered.map((tool) => {
                const status = toolStatus(tool);
                const isOpen = expanded === tool.name;
                return (
                  <Card
                    className={styles.toolCard}
                    styles={{ body: { padding: 0 } }}
                    key={tool.name}
                    onClick={() => setExpanded(isOpen ? null : tool.name)}
                  >
                    <div className={styles.toolCardHead}>
                      <span className={styles.toolIcon}>
                        <ToolOutlined />
                      </span>
                      <StatusDot tone={status.tone} label={status.label} />
                    </div>
                    <div className={styles.toolCardName}>{tool.name}</div>
                    <div className={styles.toolCardDescription}>
                      {tool.description || '未提供描述'}
                    </div>
                    <div className={styles.toolCardFoot}>
                      <Tag>{tool.source === 'local' ? '本地' : tool.source}</Tag>
                      <span>参数 Schema</span>
                      <Button
                        type="text"
                        size="small"
                        icon={<SettingOutlined />}
                        onClick={(event) => {
                          event.stopPropagation();
                          setExpanded(isOpen ? null : tool.name);
                        }}
                        aria-label={`查看 ${tool.name} 参数`}
                      />
                    </div>
                    {isOpen ? (
                      <pre className={styles.schema}>{JSON.stringify(tool.parameters, null, 2)}</pre>
                    ) : null}
                  </Card>
                );
              })}
            </div>
          )}
        </>
      )}

      {/* ---- 上传弹窗 ---- */}
      <Modal
        title="注册工具"
        open={uploadOpen}
        onCancel={() => setUploadOpen(false)}
        onOk={() => void handleUpload()}
        okText="上传并接入"
        cancelText="取消"
        confirmLoading={uploading}
        destroyOnClose
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <div>
            <input
              ref={fileRef}
              type="file"
              hidden
              accept=".py"
              onChange={(event) => {
                const picked = event.target.files?.[0] ?? null;
                setFile(picked);
                if (picked && !name) setName(guessToolName(picked.name));
                event.target.value = '';
              }}
            />
            <Button icon={<ToolOutlined />} onClick={() => fileRef.current?.click()}>
              {file ? file.name : '选择 .py 文件'}
            </Button>
            <div className={styles.sectionDescription} style={{ marginTop: 6 }}>
              单个 Python 文件，最多 256KB；只依赖标准库与 mcp SDK。
            </div>
          </div>
          <Input
            addonBefore="名字"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="小写字母开头，3-32 位，只含小写/数字/下划线"
          />
          <Input
            addonBefore="显示名"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder="留空则用名字"
          />
          <Input.TextArea
            rows={2}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="这个工具是做什么的（只给人看）"
          />
        </Space>
      </Modal>

      {/* ---- 试跑弹窗 ---- */}
      <Modal
        title={probe ? `试跑 · ${probe.server.display_name}` : '试跑'}
        open={probe !== null}
        onCancel={() => setProbe(null)}
        onOk={() => void handleProbe()}
        okText="调用"
        cancelText="关闭"
        confirmLoading={probe?.running ?? false}
        width={640}
      >
        {probe ? (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Select
              style={{ width: '100%' }}
              value={probe.tool}
              onChange={(value) => setProbe({ ...probe, tool: value, output: '' })}
              options={probe.server.tools.map((tool) => ({ value: tool, label: tool }))}
            />
            <Input.TextArea
              rows={3}
              value={probe.args}
              onChange={(event) => setProbe({ ...probe, args: event.target.value })}
              placeholder='{"text": "你好"}'
            />
            <div className={styles.sectionDescription}>
              走的是 Agent 调用同一条路径（execute_tool → 沙箱容器），返回工具的原始输出。
            </div>
            {probe.output ? <pre className={styles.schema}>{probe.output}</pre> : null}
          </Space>
        ) : null}
      </Modal>
    </div>
  );
}
