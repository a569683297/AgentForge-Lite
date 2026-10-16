import styles from '../styles/dataPages.module.css';

/**
 * 信息悬浮说明的共享组件（块 4 从 EvalPage 提取——评测区与分析区共用同一套样式/结构）。
 *
 * 项目惯例（DESIGN「口径必须写在 UI 上」）：任何展示口径数字的地方都配一个 info 图标，
 * 悬浮给出「它是什么 → 怎么算的 → 要注意什么」三步 + 一句备注。
 * 图标包裹样式由各调用点自己控制（运行卡 / 面板标题 / 指标卡各不相同），
 * 这里只保证**内容结构**统一。
 */

/** 悬浮说明的数据形状：标题 + 步骤列表 + 备注（与评测页原定义逐字一致）。 */
export interface InfoTip {
  title: string;
  steps: string[];
  note: string;
}

export function InfoTipContent({ tip }: { tip: InfoTip }) {
  return (
    <div className={styles.flowTip}>
      <div className={styles.flowTipTitle}>{tip.title}</div>
      <div className={styles.flowTipSteps}>
        {tip.steps.map((step, index) => (
          <div className={styles.flowTipStep} key={step}>
            <span className={styles.flowTipIndex}>{index + 1}</span>
            <span>{step}</span>
          </div>
        ))}
      </div>
      <div className={styles.flowTipNote}>{tip.note}</div>
    </div>
  );
}
