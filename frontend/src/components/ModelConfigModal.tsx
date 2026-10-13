import { ArrowLeftOutlined, DeleteOutlined, EditOutlined, PlusOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { Button, Input, Modal, Popconfirm, Space, Switch, message } from 'antd';
import { useState } from 'react';
import { createModel, deleteModel, testModel, testSavedModel, updateModel } from '../api/models';
import type { ModelItem, ModelTestResult } from '../types/api';
import { ModelKindTag } from './ModelKindTag';
import styles from '../styles/modelSwitcher.module.css';

/**
 * 自定义模型配置弹窗（需求稿 §3.2）。
 *
 * 双视图结构：列表（管理已有）↔ 表单（新建/编辑）。
 * 表单为什么不用 antd Form：本弹窗只需要"4 个字符串 + 两条手写校验"，
 * 受控 state 比 Form 的实例/字段路径模型更直接（项目别处也无 Form 先例）。
 *
 * 连通性测试（C4）的分叉语义 —— 这个分叉是刻意的，不是省事：
 *   · 新建态：测**表单值**（模型还不存在，没有别的可测）；
 *   · 编辑态且 key 留空：测**已保存的配置**（testSavedModel）——
 *     因为表单的 key 字段按约定"留空=不修改"，此时表单值 ≠ 真实配置
 *     （真实 key 在库里），拿表单值去测会得到一个必然失败且误导的 401；
 *   · 编辑态且填了新 key：测表单值（用户想换 key，就该测新 key）。
 */

/** 表单四个字段（全 string 受控；api_key 空串在两种语义间自有分界，见上） */
interface ModelFormValues {
  name: string;
  base_url: string;
  api_key: string;
  model: string;
}

const EMPTY_FORM: ModelFormValues = { name: '', base_url: '', api_key: '', model: '' };

export function ModelConfigModal({ open, models, onClose, onModelsChanged }: {
  open: boolean;
  models: ModelItem[];
  onClose: () => void;
  onModelsChanged: () => void;
}) {
  const [formVisible, setFormVisible] = useState(false);
  const [editing, setEditing] = useState<ModelItem | null>(null);
  const [form, setForm] = useState<ModelFormValues>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ModelTestResult | null>(null);

  const customModels = models.filter((model) => model.kind !== 'builtin');

  function resetForm() {
    setFormVisible(false);
    setEditing(null);
    setForm(EMPTY_FORM);
    setTestResult(null);
  }

  function handleClose() {
    resetForm();
    onClose();
  }

  function openCreate() {
    setEditing(null);
    setForm(EMPTY_FORM);
    setTestResult(null);
    setFormVisible(true);
  }

  function openEdit(item: ModelItem) {
    setEditing(item);
    // key 字段**不回显**（连打码值都不填进去）：它与 PATCH 的"留空=不修改"约定互锁 ——
    // 回显什么都可能被用户误编辑，空 + 提示"当前：sk-***c4"是最不会出错的形态
    setForm({ name: item.name, base_url: item.base_url ?? '', api_key: '', model: item.model });
    setTestResult(null);
    setFormVisible(true);
  }

  /** 表单校验（与后端同口径的最小集：必填 + URL 前缀） */
  function validate(): boolean {
    if (!form.name.trim() || !form.base_url.trim() || !form.model.trim()) {
      message.warning('显示名、Base URL、模型标识均为必填');
      return false;
    }
    if (!/^https?:\/\//i.test(form.base_url.trim())) {
      message.warning('Base URL 需以 http:// 或 https:// 开头');
      return false;
    }
    return true;
  }

  async function handleSave() {
    if (!validate()) return;
    setSaving(true);
    try {
      const base = { name: form.name.trim(), base_url: form.base_url.trim(), model: form.model.trim() };
      // api_key 只在**填了**的时候才进请求体（留空 = 不修改 / 不设置，与后端约定一致）
      const payload = { ...base, ...(form.api_key ? { api_key: form.api_key } : {}) };
      if (editing) {
        await updateModel(editing.id, payload);
        message.success('已保存');
      } else {
        await createModel(payload);
        message.success('已创建');
      }
      onModelsChanged();
      resetForm();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : '保存失败');
    } finally {
      setSaving(false);
    }
  }

  async function handleTest() {
    if (!validate()) return;
    setTesting(true);
    setTestResult(null);
    try {
      const result = editing && !form.api_key
        ? await testSavedModel(editing.id)
        : await testModel({
            base_url: form.base_url.trim(),
            model: form.model.trim(),
            ...(form.api_key ? { api_key: form.api_key } : {}),
          });
      setTestResult(result);
    } catch (reason) {
      // HTTP 层错误（如 422 校验失败）——转成同形的结果对象，展示路径统一
      setTestResult({ ok: false, latency_ms: null, sample: null, error: reason instanceof Error ? reason.message : '测试请求失败' });
    } finally {
      setTesting(false);
    }
  }

  async function handleDelete(model: ModelItem) {
    try {
      await deleteModel(model.id);
      message.success('已删除');
      onModelsChanged();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : '删除失败');
    }
  }

  async function handleToggle(model: ModelItem, enabled: boolean) {
    try {
      await updateModel(model.id, { enabled });
      onModelsChanged();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : '操作失败');
    }
  }

  return (
    <Modal
      open={open}
      onCancel={handleClose}
      footer={null}
      width={620}
      centered
      title={formVisible ? (editing ? `编辑模型 · ${editing.name}` : '新建自定义模型') : '自定义模型配置'}
    >
      {formVisible ? (
        <div className={styles.configForm}>
          <button className={styles.backLink} onClick={resetForm}>
            <ArrowLeftOutlined /> 返回列表
          </button>

          <label className={styles.field}>
            <span className={styles.fieldLabel}>显示名</span>
            <Input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="如：我的 Qwen" maxLength={255} />
          </label>

          <label className={styles.field}>
            <span className={styles.fieldLabel}>Base URL</span>
            <Input value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} placeholder="https://api.example.com/v1" maxLength={500} />
            <span className={styles.fieldHint}>OpenAI 兼容端点根地址；本机 Ollama 填 http://host.docker.internal:11434/v1</span>
          </label>

          <label className={styles.field}>
            <span className={styles.fieldLabel}>API Key</span>
            <Input value={form.api_key} onChange={(event) => setForm({ ...form, api_key: event.target.value })} placeholder={editing ? '留空则不修改' : '本地端点可留空'} maxLength={500} autoComplete="off" />
            <span className={styles.fieldHint}>
              {editing && editing.api_key_masked
                ? `当前：${editing.api_key_masked}（留空 = 保持原 key 不变）`
                : '本地端点（如 Ollama）通常不需要 key'}
            </span>
          </label>

          <label className={styles.field}>
            <span className={styles.fieldLabel}>模型标识</span>
            <Input value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} placeholder="如：qwen-max" maxLength={255} />
            <span className={styles.fieldHint}>发给端点的真实模型名（请求体里的 model 字段）</span>
          </label>

          {testResult ? (
            <div className={testResult.ok ? styles.testResultOk : styles.testResultFail}>
              {testResult.ok
                ? `✓ 连接成功（${testResult.latency_ms}ms）${testResult.sample ? ` · 模型回复：${testResult.sample}` : ''}`
                : `✗ 连接失败：${testResult.error}`}
            </div>
          ) : null}

          <div className={styles.formFooter}>
            <Button icon={<ThunderboltOutlined />} onClick={() => void handleTest()} loading={testing}>
              测试连接
            </Button>
            <Space size={8}>
              <Button onClick={resetForm}>取消</Button>
              <Button type="primary" onClick={() => void handleSave()} loading={saving}>
                {editing ? '保存' : '创建'}
              </Button>
            </Space>
          </div>
        </div>
      ) : (
        <div>
          <p className={styles.listIntro}>
            自定义模型保存在后端（对话由后端发起，key 放前端没有用）。支持任何 OpenAI 兼容端点：云 API、中转站、本机 Ollama。
          </p>
          <div className={styles.modalList}>
            {customModels.length ? (
              customModels.map((model) => (
                <div className={`${styles.configRow} ${model.enabled ? '' : styles.configRowOff}`} key={model.id}>
                  <div className={styles.configRowMain}>
                    <div className={styles.configRowName}>
                      <strong>{model.name}</strong>
                      <ModelKindTag kind={model.kind} />
                      {model.enabled ? null : <span className={styles.disabledHint}>已停用</span>}
                    </div>
                    <div className={styles.configRowMeta}>
                      {model.model} · {model.base_url ?? '—'} · key：{model.api_key_masked ?? '未设置'}
                    </div>
                  </div>
                  <Switch
                    size="small"
                    checked={model.enabled}
                    onChange={(checked) => void handleToggle(model, checked)}
                    aria-label={model.enabled ? '停用' : '启用'}
                  />
                  <Button size="small" type="text" icon={<EditOutlined />} onClick={() => openEdit(model)} aria-label="编辑" />
                  <Popconfirm
                    title="删除这个模型？"
                    description="不影响已有会话历史；若某会话正选中它，将回退为默认（自动降级）。"
                    onConfirm={() => void handleDelete(model)}
                    okText="删除"
                    okButtonProps={{ danger: true }}
                    cancelText="取消"
                  >
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} aria-label="删除" />
                  </Popconfirm>
                </div>
              ))
            ) : (
              <div className={styles.empty}>
                还没有自定义模型。点下方按钮添加第一个 —— 例如本机 Ollama：http://host.docker.internal:11434/v1
              </div>
            )}
          </div>
          <Button className={styles.addButton} type="dashed" block icon={<PlusOutlined />} onClick={openCreate}>
            新建自定义模型
          </Button>
        </div>
      )}
    </Modal>
  );
}
