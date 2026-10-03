"""
AgentForge 配置中心
==================
所有配置统一从这里读取（.env 驱动），业务代码不得直接读 os.environ。

为什么必须这样设计（面试考点）：
1. 单一事实来源：改一处生效全项目，避免配置散落各处
2. 环境隔离：dev/prod 通过 .env 切换，代码零改动
3. 类型安全：pydantic-settings 自动做类型转换和校验，写错类型启动即报错
4. 分层红线：这是阶段 3 加多通道/多环境不返工的基础
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 检索策略三配置（D19）。定义在 config 层而不是 service 层：
# ① 它是**唯一合法取值清单**，必须有一个权威出处（service 与校验器都从这里取）
# ② 校验器要在 settings 构造时就拦住非法值，不能等到检索时才发现拼错了
RETRIEVER_CONFIGS: tuple[str, ...] = ("pure_vector", "hybrid", "hybrid_rerank")

# LLM 通道名（D22）。存在的理由与 RETRIEVER_CONFIGS 完全对称：
# ① 它是**唯一合法取值清单**，网关、配置校验、judge 三处都从这里取，不各写一份
# ② 校验器要在 settings 构造时就拦住拼错的名字，不能等到跑评测时才发现
LLM_CHANNELS: tuple[str, ...] = ("deepseek", "openai")

# Harness 写操作的人工确认阈值（D27）。存在的理由与前两个常量完全对称：
# 它是**唯一合法取值清单**，配置校验与文档都从这里取，不各写一份。
HARNESS_RISK_LEVELS: tuple[str, ...] = (
    "none",
    "low_write",
    "medium_write",
    "high_write",
    "all",
)


class Settings(BaseSettings):
    """全局配置。字段名与 .env 中的键一一对应（不区分大小写）。"""

    model_config = SettingsConfigDict(
        env_file=".env",        # 从项目根目录 .env 读取
        env_file_encoding="utf-8",
        extra="ignore",         # .env 里多余的键不报错
    )

    # ---- 应用 ----
    app_env: str = "dev"
    log_level: str = "INFO"

    # ---- LLM 提供商 ----
    llm_provider: str = "deepseek"          # deepseek | openai
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_chat_model: str = "deepseek-chat"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_chat_model: str = "gpt-4o-mini"

    # ---- Embedding（向量化，D9 新增）----
    # 注意：DeepSeek 没有 embeddings 接口（实测 /embeddings 返回 404），
    # 所以 embedding 必须独立配置，与 chat 通道解耦。
    embedding_provider: str = "local"       # local（fastembed 本地模型）| openai
    embedding_model: str = "BAAI/bge-small-zh-v1.5"   # 本地模型名
    embedding_dim: int = 512                # 向量维度（必须与模型一致！切换模型时要重建索引）
    openai_embed_model: str = "text-embedding-3-small"  # openai 方案的模型名

    # ---- 检索策略（D19：三配置可切，供消融实验复用）----
    # 存在的理由不是"上线时换一种"，而是**同一个评测集要能跑三遍**：
    # PRD §9.5 的消融矩阵 A/B/C（S2 验收：混合+重排 vs 纯向量 ≥ +10%）。
    # 三条链路必须同时存在且可归因，否则"重排到底有没有用"永远答不出来。
    retriever_config: str = "hybrid_rerank"   # pure_vector | hybrid | hybrid_rerank

    # ---- 重排（D19）----
    # 模型走本地 ONNX（fastembed 内置 cross-encoder），首次自动下载到 models/
    # ⚠ 国内首次下载需 HF_ENDPOINT=https://hf-mirror.com（HF 直连不通）
    rerank_model: str = "BAAI/bge-reranker-base"
    # 超时阈值（秒）。实测生产尺寸（300 字/切片、20 候选）750~820ms（load≤10），
    # 机器繁忙时可到 1.4s —— 3s 约合 3.7 倍余量。
    # ⚠ 这个值是**拍的**，不是调出来的；等 D20-D24 有评测/压测数据后应回头校正。
    rerank_timeout_s: float = 3.0

    # ---- 评测 judge（D22）----
    # judge 是评测的**量具**，不是被测对象 —— 它必须能锁死、能写进报告。
    # D20 实测选型：deepseek 0.84s/次、0 重试、100% 成功；
    #   中转 GPT 16.46s/次（慢 19.6 倍）、失败率在 1%~37% 之间飘。
    #   所以默认用 deepseek，中转仅作交叉验证（跑 probe 时显式指定）。
    judge_model: str = "deepseek"      # 取值见 LLM_CHANNELS
    judge_runs_per_case: int = 3       # 每条题重复打分次数（D20 数据支撑的 3 次多数投票）
    judge_max_concurrency: int = 4     # 并发上限：中转站不宜压满，也避免本机 load 失控
    judge_timeout_s: float = 90.0

    # ---- 基础设施 ----
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "agentforge"
    postgres_password: str = ""
    postgres_db: str = "agentforge"

    redis_host: str = "localhost"
    redis_port: int = 6379

    # ---- 可观测（Langfuse）----
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ---- MCP 客户端（D27）----
    # 总开关。关掉 = 不接任何外部 server，注册表只剩项目内静态注册的工具。
    mcp_enabled: bool = True

    # 外部 server（Harness 官方 MCP）的开关，**默认关**。
    # 为什么默认关：它每次启动都要拉一个常驻 npm 子进程（冷启动可能几分钟），
    # 而绝大多数开发/回归根本用不到它。演示与验收时才打开 —— 这也正是 PRD 里
    # 「scripts/warmup_mcp.py 预热、演示前必跑」这条要求的由来。
    mcp_harness_enabled: bool = False
    mcp_harness_command: str = "npx"
    # 空格分隔即可 —— 不引 shlex，因为这里有 shell 元字符的风险为零（我们自己配的）
    mcp_harness_args: str = "-y harness-mcp-v2@latest"
    # ⚠ 国内环境必须走镜像：实测直连 registry.npmjs.org **超时**（curl 12s 返回 000），
    #   加镜像后 2 分 31 秒拉完；不加会挂住 5 分钟以上且没有任何输出。
    mcp_npm_registry: str = "https://registry.npmmirror.com"
    # **只读的正解**：个人 PAT 做不到只读（它继承账号全权，创建时没有只读档），
    # 只读靠服务端这个变量屏蔽写操作（create/update/delete/execute）。
    mcp_harness_read_only: bool = True
    # 写操作的人工确认阈值：none=全部要确认（最保守）/ low_write / medium_write / high_write / all
    mcp_harness_auto_approve_risk: str = "none"

    # Harness 凭证。**只从 .env 读，绝不入库、绝不打印**。
    # 格式 `pat.<accountId>.<tokenId>.<secret>`；第二段才是 API 层的 accountId，
    # 与界面上那个数字账号名不是一回事（用错会得到 403 account identifier mismatch）。
    harness_api_key: str = ""

    # ---- 便捷属性（拼好的连接串，业务层直接用）----
    @property
    def database_url(self) -> str:
        """SQLAlchemy async 连接串（D2 起使用）。"""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        """Redis 连接串。"""
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @property
    def primary_llm(self) -> dict:
        """返回当前主通道配置（chat 用）。"""
        if self.llm_provider == "openai":
            return {
                "api_key": self.openai_api_key,
                "base_url": self.openai_base_url,
                "model": self.openai_chat_model,
            }
        return {
            "api_key": self.deepseek_api_key,
            "base_url": self.deepseek_base_url,
            "model": self.deepseek_chat_model,
        }

    @property
    def backup_llm(self) -> dict | None:
        """返回备用通道配置（主通道失败时降级用，无 key 则返回 None）。"""
        if self.llm_provider == "deepseek" and self.openai_api_key:
            return {
                "api_key": self.openai_api_key,
                "base_url": self.openai_base_url,
                "model": self.openai_chat_model,
            }
        return None

    def llm_channel(self, name: str) -> dict:
        """
        按名字取一个 LLM 通道配置（D22：judge 必须**锁定通道**）。

        为什么 judge 不能走 `primary_llm` / `backup_llm` 那条自动降级的路：
            降级的语义是"主通道失败 → 静默切备用"。judge 若走它，会出现
            **一部分样本是 deepseek 打的、一部分是中转 GPT 打的**，
            而报告上只写一个 judge 名字 → 分数不可比、且没有任何报错。
            这与 D19 那条"降级必须可归因"是同一个问题，只是换了一层
            （那次是检索配置，这次是打分模型）。

            ⚠ 名字里的 "openai" 指的是**OpenAI 兼容协议通道**，本项目实际
              把它配成中转站（`openai_base_url` 指向中转）。D20 探针脚本里
              叫它 `relay-gpt`，那是脚本层的显示别名，配置层统一用协议名。

        非法名字**直接抛错**，不静默回退到主通道 —— 回退会让 `.env` 里
        写错的名字看起来"工作正常"，只是分数悄悄来自另一个模型。
        （同 `_validate_retriever_config` 的理由，方向一致。）
        """
        normalized = (name or "").strip().lower()
        if normalized == "deepseek":
            return {
                "api_key": self.deepseek_api_key,
                "base_url": self.deepseek_base_url,
                "model": self.deepseek_chat_model,
            }
        if normalized == "openai":
            return {
                "api_key": self.openai_api_key,
                "base_url": self.openai_base_url,
                "model": self.openai_chat_model,
            }
        raise ValueError(f"未知 LLM 通道 {name!r}，可选：{LLM_CHANNELS}")

    @property
    def embedding_config(self) -> dict:
        """
        返回当前 embedding 方案配置（D9 新增）。

        设计意图：embedding 层可切换（本地模型 ↔ API），业务代码只读这里，
        换 provider 只改 .env，代码零改动。

        Returns:
            {"provider": "local"|"openai", "model": 模型名, "dim": 维度, ...}
        """
        if self.embedding_provider == "openai":
            return {
                "provider": "openai",
                "model": self.openai_embed_model,
                "dim": 1536,                       # text-embedding-3-small 维度
                "api_key": self.openai_api_key,
                "base_url": self.openai_base_url,
            }
        return {
            "provider": "local",
            "model": self.embedding_model,
            "dim": self.embedding_dim,
        }


    @field_validator("retriever_config")
    @classmethod
    def _validate_retriever_config(cls, value: str) -> str:
        """
        拦非法检索配置 —— **在启动时**拦，不是等第一次检索时才发现。

        为什么必须在这里拦（而不是在 retrieve() 里兜底回退）：
        .env 里写 `RETRIEVER_CONFIG=hybrid_rerankk`（多打一个 k）时，
        兜底回退会让系统**照常启动、静默用另一个策略跑**——
        于是消融实验里那一列的配置是假的，而没有任何报错提示你。
        这类"配置写错 → 结果悄悄变成另一件事"正是 PRD §3.4 要防的。

        顺手做规范化（去空白 + 转小写）：`Hybrid ` 这种写法应当被接受，
        不值得让人因为大小写去 grep 日志。
        """
        normalized = value.strip().lower()
        if normalized not in RETRIEVER_CONFIGS:
            raise ValueError(
                f"retriever_config 只能是 {RETRIEVER_CONFIGS} 之一，收到 {value!r}"
            )
        return normalized

    @field_validator("judge_model")
    @classmethod
    def _validate_judge_model(cls, value: str) -> str:
        """拦非法 judge 通道名 —— 理由与 `_validate_retriever_config` 完全一致。

        judge 写错的后果比检索配置写错更隐蔽：分数照常产出、报告照常生成，
        只是**打分的人换了**。评测的第一个问题就是"谁打的、打了几次"，
        这个字段承担不了"看起来填了但其实是别的模型"。
        """
        normalized = value.strip().lower()
        if normalized not in LLM_CHANNELS:
            raise ValueError(
                f"judge_model 只能是 {LLM_CHANNELS} 之一，收到 {value!r}"
            )
        return normalized

    @field_validator("mcp_harness_auto_approve_risk")
    @classmethod
    def _validate_harness_risk(cls, value: str) -> str:
        """
        拦非法写操作阈值 —— 理由与上面两条完全一致，但**后果更重**。

        这个值决定"哪些写操作可以不问人就执行"。写错（比如把 `none` 打成 `all`）
        的后果是**防护静默降级**：系统照常启动、照常跑，只是把危险操作放行了。
        `.env` 里少打一个字母，换来的是一道门没了 —— 必须在启动时就拦住。
        """
        normalized = value.strip().lower()
        if normalized not in HARNESS_RISK_LEVELS:
            raise ValueError(
                f"mcp_harness_auto_approve_risk 只能是 {HARNESS_RISK_LEVELS} 之一，"
                f"收到 {value!r}"
            )
        return normalized


@lru_cache
def get_settings() -> Settings:
    """缓存配置实例（模块加载一次，避免重复读 .env）。"""
    return Settings()


# 模块级单例：业务代码 `from app.config import settings` 即可用
settings = get_settings()
