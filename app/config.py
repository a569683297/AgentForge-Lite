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

from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    """缓存配置实例（模块加载一次，避免重复读 .env）。"""
    return Settings()


# 模块级单例：业务代码 `from app.config import settings` 即可用
settings = get_settings()
