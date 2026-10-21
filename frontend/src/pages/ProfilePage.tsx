import { DeleteOutlined, ReloadOutlined, SaveOutlined, UserOutlined } from '@ant-design/icons';
import { Button, Card, Input, Popconfirm, Skeleton, Tooltip, message } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { deleteProfileFact, getProfile, patchProfile, type ProfileFact, type ProfilePayload } from '../api/profile';
import { ApiError } from '../api/client';
import { EmptyState, ErrorState } from '../components/EmptyState';
import { PageHeader } from '../components/PageHeader';
import { formatDateTime } from '../lib/format';
import styles from '../styles/profile.module.css';

/**
 * 我的画像（所有登录用户可见）
 * ============================
 * 这一页不是"设置项"，而是这个功能的**正确性前提**：
 *
 *   画像写错比没有画像更糟 —— Agent 会一口咬定一个错误的事实，
 *   而用户找不到地方纠正，只能怀疑"这系统是不是坏了"。
 *
 * 所以页面必须让三件事同时成立：
 *   ① **看得见**：每条自动提取的事实都列出来，并标出处（来自哪次会话）
 *   ② **改得动**：自己填的字段直接编辑保存
 *   ③ **删得掉**：自动提取的只能删，不能改 —— 改了会引出"这算显式还是隐式"的无解问题
 *
 * 两个刻意的做法：
 *   · 不做乐观更新（保存/删除都等后端返回）。这一页的每一个字都会被下次对话读到，
 *     "界面显示已保存、实际没存上"的代价是 Agent 继续按旧画像回答。
 *   · 空态要解释"为什么空"，而不是只显示"暂无数据" —— 否则用户不知道
 *     是自己没填、还是功能坏了。
 */

const KIND_LABEL: Record<string, string> = {
  preference: '偏好',
  fact: '事实',
  goal: '目标',
};

export function ProfilePage() {
  const [payload, setPayload] = useState<ProfilePayload | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await getProfile();
      setPayload(data);
      setValues(data.explicit);
    } catch (reason: unknown) {
      setError(explain(reason, '无法加载画像'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // 脏检查：只在真的改了东西时才让「保存」可点。
  // 为什么不干脆一直可点：保存按钮常亮会让人以为"每次都得点一下"，
  // 而这一页的保存是整体替换 —— 误点一次就把表单里的旧值又写回去了（无害但困惑）。
  const dirty = payload
    ? payload.fields.some((field) => (values[field.key] ?? '') !== (payload.explicit[field.key] ?? ''))
    : false;

  async function save() {
    if (!payload) return;
    setSaving(true);
    try {
      // ⚠ 提交**全部**字段（含空串）：后端 PATCH 语义下「没传」= 保持不变，
      //   「传空串」= 删除。只提交改动过的那些，会让"清空输入框"永远不生效。
      const saved = await patchProfile(values);
      setPayload((prev) => (prev ? { ...prev, explicit: saved } : prev));
      setValues(saved);
      message.success('已保存，下一轮对话起生效');
    } catch (reason: unknown) {
      message.error(explain(reason, '保存失败'));
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: number) {
    setDeletingId(id);
    try {
      await deleteProfileFact(id);
      setPayload((prev) => (prev ? { ...prev, facts: prev.facts.filter((f) => f.id !== id) } : prev));
      message.success('已删除，之后不会再带上这条');
    } catch (reason: unknown) {
      message.error(explain(reason, '删除失败'));
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <>
      <PageHeader
        title="我的画像"
        description="Agent 在不同会话里都会带上这些信息：你填的每次全量带上，自动提取的只带相关的几条"
      />

      <p className={styles.intro}>
        <strong>画像的作用范围是「你」而不是「这个会话」</strong> —— 换一个新对话它依然成立。
        上半部分（<UserOutlined /> 关于你）由你自己填写，<strong>每轮对话都会全量带上</strong>；
        下半部分由 Agent 从你过去的发言里自动提取，<strong>只会带与本轮问题最相关的几条</strong>。
        自动提取的内容可能不准，<strong>写得不准比没有更糟</strong>，所以每条都给了你否决权。
        改动从<strong>下一轮</strong>对话开始生效（正在进行的那一轮不受影响）。
      </p>

      {loading ? <Skeleton active paragraph={{ rows: 6 }} /> : null}
      {error ? <ErrorState message={error} action="重试" onAction={() => void load()} /> : null}

      {!loading && !error && payload ? (
        <div className={styles.grid}>
          <Card
            size="small"
            title={<span><UserOutlined /> 关于你（你自己填的）</span>}
            className={styles.cardBody}
          >
            <div className={styles.twoCol}>
              {payload.fields.map((field) => (
                <div key={field.key}>
                  <div style={{ marginBottom: 4, fontSize: 12 }}>{field.label}</div>
                  {field.key === 'answer_style' ? (
                    // 回答风格通常是一句话（"直接给结论，少铺垫"），单行输入太挤。
                    // 按 key 特判而不是按长度猜：长度是可调参数，猜出来的行为会随配置漂移。
                    <Input.TextArea
                      value={values[field.key] ?? ''}
                      maxLength={field.max_length}
                      autoSize={{ minRows: 2, maxRows: 4 }}
                      placeholder={field.hint}
                      onChange={(event) => setValues((prev) => ({ ...prev, [field.key]: event.target.value }))}
                    />
                  ) : (
                    <Input
                      value={values[field.key] ?? ''}
                      maxLength={field.max_length}
                      placeholder={field.hint}
                      onChange={(event) => setValues((prev) => ({ ...prev, [field.key]: event.target.value }))}
                    />
                  )}
                  <span className={styles.fieldHint}>{field.hint}</span>
                </div>
              ))}
            </div>
            <div className={styles.saveBar}>
              <span className={styles.saveHint}>
                {dirty ? '有未保存的修改' : '清空某个输入框并保存 = 删除这个字段'}
              </span>
              <Button
                type="primary"
                icon={<SaveOutlined />}
                loading={saving}
                disabled={!dirty}
                onClick={() => void save()}
              >
                保存
              </Button>
            </div>
          </Card>

          <Card
            size="small"
            title={<span>对话里了解到的事（{payload.facts.length}）</span>}
            extra={<Button size="small" type="text" icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>}
            className={styles.cardBody}
          >
            {payload.facts.length === 0 ? (
              <EmptyState message="还没有自动提取到内容。Agent 会在你累计说满 6 句话后尝试提取一次，且只提取「换个新会话仍然成立」的信息（职业、偏好、正在做的事），一次性的提问不会被记下来。" />
            ) : (
              <div className={styles.factList}>
                {payload.facts.map((fact) => (
                  <FactRow
                    key={fact.id}
                    fact={fact}
                    deleting={deletingId === fact.id}
                    onDelete={() => void remove(fact.id)}
                  />
                ))}
              </div>
            )}
          </Card>
        </div>
      ) : null}
    </>
  );
}

function FactRow({ fact, deleting, onDelete }: { fact: ProfileFact; deleting: boolean; onDelete: () => void }) {
  return (
    <div className={styles.factRow}>
      <span className={styles.factKind}>{KIND_LABEL[fact.kind] ?? fact.kind}</span>
      <div>
        <div className={styles.factText}>{fact.text}</div>
        <div className={styles.factMeta}>
          <Tooltip title="模型自评的可信度，仅用于展示与排序，不代表一定正确">
            <span>
              <span className={styles.confidenceBar}>
                <span style={{ width: `${Math.round(fact.confidence * 100)}%` }} />
              </span>
              可信度 {fact.confidence.toFixed(2)}
            </span>
          </Tooltip>
          {/* 来源可追溯：让用户能判断"它为什么这么认为" */}
          {fact.source_session_id ? (
            <Link to={`/chat/${fact.source_session_id}`}>来自这次对话</Link>
          ) : (
            <span>来源会话已删除</span>
          )}
          {fact.created_at ? <span>{formatDateTime(fact.created_at)}</span> : null}
        </div>
      </div>
      <Popconfirm
        title="删除这条画像？"
        description="删除后 Agent 不会再带上它。"
        okText="删除"
        cancelText="取消"
        onConfirm={onDelete}
      >
        <Button size="small" type="text" danger icon={<DeleteOutlined />} loading={deleting} />
      </Popconfirm>
    </div>
  );
}

function explain(reason: unknown, fallback: string): string {
  if (reason instanceof ApiError) return reason.message;
  return fallback;
}
