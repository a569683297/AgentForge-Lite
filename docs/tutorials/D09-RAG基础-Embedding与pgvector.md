# D9 复习教程：RAG 基础（Embedding + pgvector 向量检索）

> 日期：2026-09-10 ｜ 项目：AgentForge 阶段1 ｜ 状态：已验收通过（语义检索三用例全命中）

---

## 1. 今日目标

学完这一天，你应该能独立回答：

1. 为什么需要 RAG？跟微调、长上下文硬塞比有什么优势？
2. RAG 的两个阶段各做什么？
3. Embedding 是什么？为什么能实现"语义匹配"？
4. 向量检索 vs 关键词检索的区别？各自强在哪里？
5. 为什么选 pgvector 而不是专门的向量数据库？
6. 切片为什么要重叠（overlap）？
7. 换 embedding 模型时，代码层和数据层各要做什么？

---

## 2. 核心概念讲解

### 2.1 为什么需要 RAG

LLM 两个知识盲区：① 不知道你的私有数据 ② 知识有截止日期。

| 方案 | 问题 |
|---|---|
| 微调 | 贵、慢、数据变了要重训 |
| 长上下文硬塞 | token 爆炸、贵、模型抓不住重点 |
| **RAG** ✅ | 提问时只检索相关片段塞进 prompt |

### 2.2 RAG 两阶段

```
【离线建库】
  文档 → 切片 → 向量化 → 存 pgvector

【在线查询】
  问题 → 向量化 → 相似度检索 top-k → 拼进 prompt → LLM 生成
```

### 2.3 Embedding（向量化）

把文本变成一串数字（向量），**语义相近 → 向量相近**。

- 维度：常见 384/512/768/1536（越高表达力越强、存储越贵）
- 原理：模型训练时学到"语义空间"，同义表达映射到相近位置
- **实测验证**（今天跑的）：
  ```
  "天气不错" vs "气象状况良好": 0.8298   ← 同义改写，高相似
  "天气不错" vs "股票大跌":     0.3862   ← 语义无关，低相似
  ```

### 2.4 向量检索 vs 关键词检索

| | 关键词 | 向量 |
|---|---|---|
| 匹配依据 | **字面**（"天气"必须出现） | **语义**（意思相近就命中） |
| 强项 | 精确术语（javascript、型号编号） | 同义改写（"休假" ↔ "年假"） |
| 弱项 | 同义不同词搜不到 | 需要 embedding 计算成本 |

**业界结论**：两者互补 → **混合检索 Hybrid Search**（向量 + 关键词，RRF 融合排序），是阶段 2 扩展点。

### 2.5 为什么用 pgvector

1. **不引入新数据库**（D2 的 PG 直接能用）→ 运维简单
2. **事务一致性**（文档、向量、业务数据同库，可事务）
3. **中小规模够用**（百万级向量性能可接受）
4. **技术选型权衡**（比"我用了 Pinecone"更能体现判断力）

D1 的前瞻性：docker-compose 当时就用了 `pgvector/pgvector:pg16` 镜像 → 今天零成本启用。

### 2.6 切片（Chunking）与重叠

**为什么切**：整篇塞进 prompt → token 爆炸 + 检索粒度太粗。

**为什么重叠**：避免把一句话从中间切断
```
"…北京是中国的" | "首都，人口众多…"    ← 不重叠，两片都读不懂
"…北京是中国的首都，人口…"             ← 重叠 50 字，两片都完整
```

### 2.7 ⚠️ 换 embedding 模型的迁移代价（用户要求"最小代价切换"）

| 层面 | 能否零成本 | 说明 |
|---|---|---|
| **代码层** | ✅ **能** | Provider 抽象 + 配置切换（改 .env 即可） |
| **数据层** | ❌ **不能** | **换模型必须重建索引** |

**为什么数据层必须重建**：
1. **维度可能不同**（bge-small-zh=512 / OpenAI small=1536）→ 列类型 `vector(N)` 不兼容
2. **向量空间不同**（语义坐标不可比）→ 即使维度相同，旧向量也失去意义

**所以"最小代价切换"的正确目标**：代码层零成本（抽象层）+ 数据层"一条命令重建"（reindex 脚本）。

---

## 3. 代码逐行解读

### 3.1 app/services/embedding_service.py（今天的设计核心）

**① 协议定义（Provider 抽象）**
```python
@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def dim(self) -> int: ...
    async def aembed(self, texts: list[str]) -> list[list[float]]: ...
```
> **Protocol vs ABC**：ABC 要求显式继承；Protocol 是**结构化类型**（方法签名对上即可，无需继承）——**类比 TS 的 interface**。

**② 本地实现 + 懒加载**
```python
def _ensure_model(self):
    if self._model is None:
        from fastembed import TextEmbedding       # import 也懒加载
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(model_name=..., cache_dir=str(MODEL_CACHE_DIR))
```
> **双重懒加载**：① `import fastembed` 放函数内（不跑 RAG 就不加载重依赖）② 模型对象首次调用才加载（90MB 下载 + 加载耗时，不拖慢启动）。**类比前端路由懒加载**。
> **cache_dir 指向项目内 `models/`**：项目自包含 + 避免写系统目录。

**③ ★ 异步包装（今天的工程重点）**
```python
async def aembed(self, texts):
    return await asyncio.to_thread(self._embed_sync, texts)
```
> **为什么必须 to_thread**：fastembed 是**同步阻塞 + CPU 密集**的库。直接在 `async def` 里调用 → **阻塞整个事件循环** → 同进程其他请求全部卡住（FastAPI 并发能力失效）。`asyncio.to_thread` 丢到线程池执行 → 事件循环继续服务其他请求。
> **一句话**：**同步阻塞库放进 async 函数，必须 to_thread 包一层**，否则 async 白写。

**④ OpenAI 实现（备用）**：证明抽象层有效——换方案不用改业务代码。

**⑤ 工厂 + 单例**：`get_embedding_provider()` 按配置返回实现（同 get_settings/get_agent 模式）。

**⑥ 业务入口**：`embed_texts()` / `embed_query()`——业务只调这两个，不关心底层。

### 3.2 app/models/document.py（向量表）
```python
embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim))
doc_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```
> - `Vector(N)` 是 pgvector 提供的列类型，**N 必须与 embedding 模型维度一致**
> - `JSONB` 存元数据（源文件、切片序号）——**为什么用 JSONB 不用 JSON**：JSONB 支持索引和字段查询
> - 没建向量索引（HNSW/IVFFlat）：小数据量顺序扫描够快，过早建索引反而增加写入成本

### 3.3 scripts/init_db.py（扩展启用）
```python
await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
```
> **必须在建表前执行**：`vector` 列类型由扩展提供，不先启用扩展 → 建表直接失败。`IF NOT EXISTS` 保证幂等。

### 3.4 app/services/retrieval_service.py（RAG 核心）

**① 切片（含重叠）**
```python
step = chunk_size - overlap          # 步长 = 片长 - 重叠
for start in range(0, len(text), step):
    chunk = text[start : start + chunk_size]
```
> 实测：700 字 → 3 片 `[300, 300, 200]`（步长 250）。

**② 入库（切片 → 批量向量化 → 批量写库）**
```python
vectors = await embed_texts(chunks)      # 批量调用比逐条快得多
for content, vec in zip(chunks, vectors, strict=True):
    session.add(Document(content=content, embedding=vec, ...))
```
> `zip(..., strict=True)`：长度不一致时抛错（防静默错位——**呼应 D6 的"贴票不能错位"**）。

**③ 检索（pgvector 余弦距离）**
```python
distance = Document.embedding.cosine_distance(query_vec)
stmt = select(...).order_by(distance).limit(top_k)
...
"similarity": round(1 - row.distance, 4)     # 距离 → 相似度
```
> **`<=>` 是 pgvector 的余弦距离算子**（值越小越相似），SQLAlchemy 里用 `.cosine_distance()`。
> **为什么转成"相似度"**：距离越小越好（反直觉），相似度越大越好（直观），展示时转换。

---

## 4. 类比迁移表

| 概念 | 前端/已学类比 |
|---|---|
| Embedding | 把文本转成"语义坐标"，像把地址转成经纬度 |
| 向量检索 | 按"距离"找最近的邻居（不是按名字找） |
| Protocol | TS 的 interface（结构化类型） |
| to_thread | Worker 线程（不阻塞主线程） |
| 懒加载模型 | 路由懒加载 / 组件按需 import |
| 切片重叠 | 分页时多带一行上下文，避免断章 |

---

## 5. 面试考点

**Q1：为什么用 RAG 而不是微调？**
→ 微调贵/慢/数据变更要重训；RAG 只需重建索引，数据实时性可控。RAG 检索相关片段拼进 prompt，成本低、可解释（能说出引用了哪段）。

**Q2：向量检索和关键词检索怎么选？**
→ 互补。向量强于同义改写，关键词强于精确术语。生产常用**混合检索**（向量+关键词 RRF 融合）。

**Q3：为什么用 pgvector 不用 Pinecone？**
→ ① 不引入新数据库（运维简单）② 事务一致性（业务数据+向量同库）③ 中小规模够用 ④ 技术选型要匹配规模，不为"用新技术"而引入复杂度。

**Q4：切片为什么要有重叠？**
→ 避免把一句话从中间切断，导致两片都语义不完整、检索到也帮不上模型。

**Q5：换 embedding 模型要做什么？**
→ 代码层零成本（Provider 抽象 + 改 .env）；**数据层必须全量重建索引**（维度可能不同 + 向量空间不可比）。

**Q6：同步的 embedding 库怎么用在 async 项目里？**
→ `asyncio.to_thread` 丢线程池。直接调用会阻塞事件循环，FastAPI 并发能力失效。

---

## 6. 自测题（附参考答案，先自己做再看）

1. 为什么需要 RAG？和微调比有什么优势？
   **参考答案**：LLM 不知道私有数据、知识有截止日期。RAG 只需建索引（不用重训模型），成本低、数据可实时更新、可解释（能说出引用了哪段）。

2. RAG 两个阶段各做什么？
   **参考答案**：离线建库——文档切片→向量化→存 pgvector；在线查询——问题向量化→相似度检索 top-k→拼进 prompt→LLM 生成。

3. Embedding 为什么能实现语义匹配？
   **参考答案**：模型训练学到语义空间，语义相近的文本映射到向量空间中相近的位置（实测："天气不错"vs"气象良好"=0.83，vs"股票大跌"=0.39）。

4. 向量检索和关键词检索各强在哪？
   **参考答案**：向量强于同义改写（"休假"能命中"年假"）；关键词强于精确术语（javascript、型号编号）。

5. 为什么选 pgvector？
   **参考答案**：不引入新数据库（运维简单）+ 事务一致性 + 中小规模够用 + 技术选型匹配规模。

6. 切片为什么要重叠？
   **参考答案**：避免把一句话从中间切断，否则两片都语义不完整，检索到也帮不上模型。

7. 换 embedding 模型要做什么（代码层 vs 数据层）？
   **参考答案**：代码层零成本（Provider 抽象 + 改 .env）；数据层必须全量重建索引（维度可能不同 + 向量空间不可比）。

---

## 7. 踩坑记录

| 坑 | 现象 | 解决 |
|---|---|---|
| 模型下载被环境拦截 | fastembed 下载模型时被工作环境的文件访问代理拦截（PermissionError） | ① 缓存目录改到项目内 `models/` ② 最终由用户在自己的终端执行一次下载；**抽象层价值体现**——真不行可零代码切 API 方案 |
| 扩展未启用 | `vector` 列类型不存在 | 建表前 `CREATE EXTENSION IF NOT EXISTS vector` |
| 同步库阻塞事件循环 | fastembed 是同步阻塞的 | `asyncio.to_thread` 包一层 |
| DeepSeek 无 embedding | `/embeddings` 返回 404（实测） | embedding 独立配置，与 chat 通道解耦 |

---

*D9 完 ｜ 下一篇：D10 检索接入 Agent（RAG 工具化：把检索做成一个工具让 Agent 调用）*
