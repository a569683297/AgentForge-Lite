import type { ModelItem } from '../types/api';
import styles from '../styles/modelSwitcher.module.css';

/**
 * 模型类型标签（内置 / 本地 / 自定义）。
 *
 * 用途说明（对照参考截图的差异）：截图里那个位置是"限时免费 / 夜间折扣"之类的
 * 计费营销标签 —— 我们没有那种数据源，编一个就是假信息；
 * 这里换成**可机器判定的事实**：builtin=来自 .env；local=base_url 指向本机
 * （localhost / host.docker.internal 等，后端按主机名判定）；custom=其余自定义。
 */
const LABEL: Record<ModelItem['kind'], string> = {
  builtin: '内置',
  local: '本地',
  custom: '自定义',
};

const CLASS: Record<ModelItem['kind'], string> = {
  builtin: styles.tagBuiltin,
  local: styles.tagLocal,
  custom: styles.tagCustom,
};

export function ModelKindTag({ kind }: { kind: ModelItem['kind'] }) {
  return <span className={`${styles.tag} ${CLASS[kind]}`}>{LABEL[kind]}</span>;
}
