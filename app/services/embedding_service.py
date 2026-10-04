"""
Embedding 服务（Provider 抽象 + 可切换）
=========================================
把"文本 → 向量"这件事抽象成统一接口，底层实现可替换。

为什么需要抽象层（用户明确要求"迁移代价最小"）：
- embedding 模型是最容易被替换的组件（效果/价格/供应商都会变）
- 业务代码只依赖 `embed()` 接口，不关心底层是本地模型还是 API
- 切换方式：改 .env 的 EMBEDDING_PROVIDER → 重启即可，**业务代码零改动**

⚠️ 迁移的物理约束（无法用设计消除，必须知道）：
- 不同模型的**向量维度不同**（bge-small-zh=512 / OpenAI small=1536）
  → 数据库列 `vector(512)` 与 1536 维不兼容
- 不同模型的**向量空间不同**（语义坐标不可比）
  → 即使维度相同，旧向量也失去意义
- 结论：**换 embedding 模型 = 必须重建整个向量索引**
  代码层可零成本切换，数据层必须 reindex（重建脚本留待后续里程碑）

实现清单：
- EmbeddingProvider：协议（接口定义）
- LocalEmbeddingProvider：本地模型（fastembed + ONNX，免费离线）
- OpenAIEmbeddingProvider：API 实现（备用方案）
- get_embedding_provider()：工厂（按配置返回，带单例缓存）
"""

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.config import settings
from app.core.logging import logger
from app.services.observability import SPAN_EMBEDDING, span

# 模型缓存目录：放在项目内（而非用户目录 ~/.cache）
# 理由：① 项目自包含，便于迁移/清理 ② 避免写入系统敏感目录
_PROJECT_ROOT = Path(__file__).resolve().parents[2]      # app/services/x.py → 项目根
MODEL_CACHE_DIR = _PROJECT_ROOT / "models"


# ============================================================
# 1. 协议：定义"什么样的对象算 embedding 提供方"
# ============================================================
@runtime_checkable
class EmbeddingProvider(Protocol):
    """
    embedding 提供方协议。

    Protocol（而非 ABC 继承）的好处：
    - 鸭子类型：任何对象只要有这两个方法就算"实现了协议"，无需显式继承
    - 类比 TS 的 interface：结构化类型，不要求继承关系
    """

    @property
    def dim(self) -> int:
        """向量维度（数据库建表要用，必须与模型一致）。"""
        ...

    async def aembed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本转成向量（异步接口，业务层统一用这个）。"""
        ...


# ============================================================
# 2. 本地模型实现（fastembed + ONNX，免费离线）
# ============================================================
class LocalEmbeddingProvider:
    """
    本地 embedding（fastembed 库 + ONNX Runtime）。

    为什么用 fastembed 而不是 sentence-transformers：
    - fastembed 基于 ONNX Runtime，**不需要 PyTorch**（torch 安装包 2GB+）
    - 模型体积小（bge-small-zh-v1.5 仅 90MB），首次自动下载后完全离线
    - RAG 场景下 ONNX 推理速度足够
    """

    def __init__(self, model_name: str, dim: int):
        self._model_name = model_name
        self._dim = dim
        self._model = None          # 懒加载：首次调用才加载（避免拖慢应用启动）

    @property
    def dim(self) -> int:
        return self._dim

    def _ensure_model(self):
        """
        懒加载模型（首次调用时才下载/加载）。

        为什么懒加载：
        - 模型加载慢（首次还要下载 90MB）+ 占内存
        - 应用启动时不该被它拖慢；不用 RAG 的场景也不该加载
        - 类比前端：路由懒加载 / 组件按需 import
        """
        if self._model is None:
            from fastembed import TextEmbedding

            MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)      # 确保目录存在
            logger.info(
                "首次加载本地 embedding 模型: %s（缓存目录 %s，可能需下载 ~90MB）",
                self._model_name,
                MODEL_CACHE_DIR,
            )
            self._model = TextEmbedding(
                model_name=self._model_name,
                cache_dir=str(MODEL_CACHE_DIR),      # ← 缓存放项目内，不写用户目录
            )
            logger.info("embedding 模型加载完成: %s", self._model_name)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        """同步实现（fastembed 本身是同步库）。"""
        self._ensure_model()
        # fastembed 返回 numpy 数组的生成器 → 转成 list[list[float]]
        return [vec.tolist() for vec in self._model.embed(texts)]

    async def aembed(self, texts: list[str]) -> list[list[float]]:
        """
        异步包装：把同步的模型推理放到线程池执行。

        ⚠️ 为什么必须这么做（重要）：
        - fastembed 是**同步阻塞**的，CPU 密集（几百毫秒到几秒）
        - 如果直接在 async 函数里调用，会**阻塞整个事件循环**
          → 同进程的其他请求全部卡住（FastAPI 的并发能力失效）
        - asyncio.to_thread 把它丢到线程池 → 事件循环继续处理其他请求
        - 这就是 D5 学的事件循环概念的实际应用
        """
        return await asyncio.to_thread(self._embed_sync, texts)


# ============================================================
# 3. OpenAI API 实现（备用方案，切换只需改 .env）
# ============================================================
class OpenAIEmbeddingProvider:
    """
    OpenAI embedding API 实现（备用）。

    当前未启用（.env 里 EMBEDDING_PROVIDER=local）。
    保留它的意义：证明抽象层有效——换方案不用改业务代码。
    """

    def __init__(self, model: str, api_key: str, base_url: str):
        self._model = model
        self._api_key = api_key
        self._base_url = base_url

    @property
    def dim(self) -> int:
        return 1536     # text-embedding-3-small 固定 1536 维

    async def aembed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
        return [item["embedding"] for item in data["data"]]


# ============================================================
# 4. 工厂：按配置返回实现（单例缓存）
# ============================================================
_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """
    返回当前 embedding 提供方（单例，按 .env 配置选择实现）。

    类比：和 get_settings() / get_agent() 同一个模式——工厂 + 缓存。
    """
    global _provider
    if _provider is None:
        cfg = settings.embedding_config
        if cfg["provider"] == "openai":
            logger.info("使用 OpenAI embedding: %s (dim=%d)", cfg["model"], cfg["dim"])
            _provider = OpenAIEmbeddingProvider(
                model=cfg["model"], api_key=cfg["api_key"], base_url=cfg["base_url"]
            )
        else:
            logger.info("使用本地 embedding: %s (dim=%d)", cfg["model"], cfg["dim"])
            _provider = LocalEmbeddingProvider(model_name=cfg["model"], dim=cfg["dim"])
    return _provider


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    业务层调用的统一入口：文本列表 → 向量列表。

    业务代码只调这个函数，永远不关心底层实现是什么。
    """
    if not texts:
        return []
    return await get_embedding_provider().aembed(texts)


async def embed_query(text: str) -> list[float]:
    """
    单条文本向量化（检索时用）。

    D28：埋点**只包这一层**，不包 `embed_texts()` ——
    因为 `embed_texts()` 也被入库路径（`document_service`）调用，那不是检索。
    只包这里顺带把两件事在 Langfuse 上分开了：
      「用户查询的向量化」（单条、每次检索都发生）
      「文档入库的向量化」（整篇、批量、只在入库时发生）
    两者的耗时口径完全不同，混在同一格会让 P95 变成一个没有意义的平均数。
    """
    with span(
        SPAN_EMBEDDING,
        as_type="embedding",
        # ★ embedding 属 generation-like 类型 → **能带 model 名**
        #   （tool / retriever 那几个类型带不了，传了会静默丢弃）。
        #   这让"换 embedding 模型之后 P95 变了吗"变成一个能按模型分组的问题。
        model=settings.embedding_config["model"],
        input={"text": text[:200]},   # 截断：问题一般很短，防异常长输入撑大 trace
    ) as obs:
        vectors = await embed_texts([text])
        # 记维度而不是向量本身：向量是 512 个浮点数，塞进 trace 既大又没有增量信息，
        # 但"维度对不对"是个真实的排障信号（换模型忘了重建索引时维度会不匹配）。
        obs.update(output={"dim": len(vectors[0]) if vectors else 0})
        return vectors[0]
