/**
 * 模型选择的**会话级记忆**（需求稿 §4.2 拍板方案：ChatGPT 式）。
 *
 * 语义（三句话讲清）：
 *   1. 每个会话**各记各的**——A 会话选 deepseek、B 会话选别的，互不干扰；
 *   2. 新建会话**继承最近一次选择**（last）；
 *   3. 值 `null` 是一个**合法状态**："默认"（不锁定任何模型，走主通道 + 自动降级）。
 *      它和"没有记录"是两回事：`null` 是用户的显式选择，"无记录"才会去继承 last。
 *
 * 为什么存 localStorage 而不是后端：这是 **UI 偏好**（我看哪个顺眼），不是业务数据；
 * 换设备不同步是已知边界（单机项目可接受，将来要跨设备再升级为后端存储）。
 *
 * 为什么密钥不在这：密钥由**后端**持有（LLM 调用发生后端，前端的 key 网关用不到）——
 * 这里只存"选中了哪个"的一个 id。
 */

const LAST_KEY = 'af:model:last';
const BY_SESSION_KEY = 'af:model:by-session';

/** `null` = 默认通道（不传 model_id，走主备降级）；string = 显式选中的模型 id */
export type ModelSelection = string | null;

interface ModelMemory {
  /** 最近一次选择（新会话继承它） */
  last: ModelSelection;
  /** 会话 id → 该会话的记忆选择 */
  bySession: Record<string, string>;
}

/** 安全读取：存储损坏（手改、旧版本格式）时按"空记忆"处理，绝不让 UI 崩在硬解析上。 */
function readMemory(): ModelMemory {
  let last: ModelSelection = null;
  try {
    const raw = window.localStorage.getItem(LAST_KEY);
    if (raw !== null) last = JSON.parse(raw) as ModelSelection;
  } catch {
    last = null;
  }

  let bySession: Record<string, string> = {};
  try {
    const raw = window.localStorage.getItem(BY_SESSION_KEY);
    if (raw) {
      const parsed: unknown = JSON.parse(raw);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        bySession = parsed as Record<string, string>;
      }
    }
  } catch {
    bySession = {};
  }

  return { last, bySession };
}

/**
 * 读：这个会话应该用哪个模型。
 *
 * 优先级：**会话自己的记录 → 最近选择（继承）→ null（默认）**。
 * `sessionId` 为 undefined（/chat 新会话页）时直接看最近选择。
 */
export function resolveModelForSession(sessionId: string | undefined): ModelSelection {
  const { last, bySession } = readMemory();
  if (sessionId && Object.prototype.hasOwnProperty.call(bySession, sessionId)) {
    return bySession[sessionId];
  }
  return last;
}

/**
 * 写：记住"这个会话选了它"。
 *
 * 一次调用同时更新两处，因为它们服务于两条不同路径：
 *   · `bySession[sessionId]` → 回到这个会话时恢复（各记各的）
 *   · `last`                 → 新会话继承（"最近选择"）
 *
 * `modelId = null` 表示**显式切回"默认"**：删掉该会话的记录（回到继承语义），
 * 同时把 last 置空——这样新会话继承到的"最近选择"也是"默认"，与用户刚做过的事一致。
 * `sessionId` 为 undefined（新会话页切换时）只更新 last。
 */
export function rememberModel(sessionId: string | undefined, modelId: ModelSelection): void {
  const { bySession } = readMemory();

  if (sessionId && modelId) {
    bySession[sessionId] = modelId;
  } else if (sessionId) {
    delete bySession[sessionId];
  }

  try {
    window.localStorage.setItem(LAST_KEY, JSON.stringify(modelId));
    window.localStorage.setItem(BY_SESSION_KEY, JSON.stringify(bySession));
  } catch {
    // localStorage 写失败（隐私模式/配额满）不应打断对话主流程 —— 记忆是增强，不是依赖
  }
}
