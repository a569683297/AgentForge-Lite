import { CheckOutlined, DownOutlined, SettingOutlined, SwapOutlined } from '@ant-design/icons';
import { Popover } from 'antd';
import { useState } from 'react';
import type { ModelItem } from '../types/api';
import type { ModelSelection } from '../lib/modelMemory';
import { ModelKindTag } from './ModelKindTag';
import styles from '../styles/modelSwitcher.module.css';

/**
 * 右上角模型切换器（S3，替换原来的只读占位）。
 *
 * 交互语义（对应需求稿 §3.1）：
 *   · 列表 = 「自动降级」（默认，value=null）+ 内置模型（.env 有 key 的）+ 自定义模型（DB）；
 *   · 打勾 = 当前选中；选定后**锁定不降级**（S2 网关语义），"自动降级"= 不传 model_id 的现状行为；
 *   · 自定义模型 `enabled=false` 置灰不可点（置灰而不删，避免误删后要重新填 key）；
 *   · 底部固定一行「配置自定义模型」→ 打开配置弹窗（§3.2）。
 *
 * 为什么不用 antd Select：需要"分组 + 每项双行（名字/模型标识）+ 标签 + 底部固定入口"
 * 这种自由版面，Select 的 option 模型表达不了 —— Popover + 自定义面板是最短路径。
 */
export function ModelSwitcher({ models, selectedModelId, onSelect, onOpenConfig }: {
  models: ModelItem[];
  selectedModelId: ModelSelection;
  onSelect: (modelId: ModelSelection) => void;
  onOpenConfig: () => void;
}) {
  const [open, setOpen] = useState(false);

  const current = models.find((model) => model.id === selectedModelId);
  // 三态显示：默认（null）/ 找到（正常名）/ 找不到（模型可能已被删除——该会话的记忆成了孤儿）
  const triggerLabel = selectedModelId === null ? '自动降级' : (current?.name ?? '未知模型');

  const builtin = models.filter((model) => model.kind === 'builtin');
  const custom = models.filter((model) => model.kind !== 'builtin');

  function choose(modelId: ModelSelection) {
    onSelect(modelId);
    setOpen(false);
  }

  const panel = (
    <div className={styles.panel}>
      <div className={styles.groupLabel}>默认</div>
      <ModelRow
        checked={selectedModelId === null}
        name="自动降级"
        model="主通道 + 备用通道"
        onChoose={() => choose(null)}
      />
      {builtin.length ? <div className={styles.groupLabel}>内置模型</div> : null}
      {builtin.map((model) => (
        <ModelRow
          key={model.id}
          checked={selectedModelId === model.id}
          name={model.name}
          model={model.model}
          kind={model.kind}
          onChoose={() => choose(model.id)}
        />
      ))}
      <div className={styles.groupLabel}>自定义模型</div>
      {custom.length ? (
        custom.map((model) => (
          <ModelRow
            key={model.id}
            checked={selectedModelId === model.id}
            name={model.name}
            model={model.model}
            kind={model.kind}
            disabled={!model.enabled}
            onChoose={() => choose(model.id)}
          />
        ))
      ) : (
        <div className={styles.empty}>还没有自定义模型 —— 从下方「配置自定义模型」添加</div>
      )}
      <div className={styles.footer}>
        <button
          className={styles.configEntry}
          onClick={() => {
            setOpen(false);
            onOpenConfig();
          }}
        >
          <SettingOutlined /> 配置自定义模型
        </button>
      </div>
    </div>
  );

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      placement="bottomRight"
      arrow={false}
      styles={{ body: { padding: 0 } }}
      content={panel}
    >
      <button className={styles.trigger} title="切换模型：默认自动降级；选定后锁定不降级（失败报错，不偷偷换）">
        <SwapOutlined className={styles.triggerCaret} />
        <span className={styles.triggerName}>{triggerLabel}</span>
        <DownOutlined className={styles.triggerCaret} />
      </button>
    </Popover>
  );
}

/** 下拉里的一行：左（名 + 模型标识，双行）/ 右（标签 + 已停用提示 + 打勾） */
function ModelRow({ name, model, kind, checked, disabled, onChoose }: {
  name: string;
  model: string;
  kind?: ModelItem['kind'];
  checked: boolean;
  disabled?: boolean;
  onChoose: () => void;
}) {
  return (
    <button
      className={`${styles.item} ${disabled ? styles.itemDisabled : ''}`}
      onClick={onChoose}
      disabled={disabled}
    >
      <span className={styles.itemMain}>
        <span className={styles.itemName}>{name}</span>
        <span className={styles.itemModel}>{model}</span>
      </span>
      {kind ? <ModelKindTag kind={kind} /> : null}
      {disabled ? <span className={styles.disabledHint}>已停用</span> : null}
      {checked ? <CheckOutlined className={styles.check} /> : null}
    </button>
  );
}
