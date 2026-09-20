# D12 复习教程：文档管理 API（上传 / 列表 / 删除）

> 对应 PRD D14「文档管理 API 全通」。
> 内部天序 D12；按 PRD 的账，D11+D12+D13 已在 D9~D11 提前吃掉，所以实际是 PRD 的 D14。

---

## 1. 今日目标

学完这天你应该能回答／做到：

1. 解释**为什么要把 `documents` 拆成两张表**（三个硬理由 + 一个反向风险）
2. 说清楚文档入库的**状态机**（processing / ready / failed）以及为什么必须异步
3. 说清楚「**为什么上传接口返回的是起点状态，而不是结果状态**」
4. 知道**四种格式的解析**分别怎么做，以及为什么解析要丢线程池
5. 独立跑通 `scripts/d12_documents_verify.py` 的 8 组用例，并解释每组在验什么

---

## 2. 核心概念讲解

### 2.1 拆表：一行到底代表什么

改之前：`documents` 表一行 = **一个切片**，但表名叫 documents。

实体层级错位带来三个具体后果：

| 后果 | 具体表现 |
|---|---|
| **字段归属错位** | `filename` / `status` 是「文档级」属性，却被摊到每一行。一份文档切 80 片，文件名就存 80 遍 |
| **聚合量无处安放** | `chunk_count` 是「这份文档切了几片」，切片表的一行没有「整体」这个视角去承载它 |
| **删除靠字符串匹配** | `DELETE FROM documents WHERE source = '员工手册.md'` |

第三条要单独说，因为它是**两个方向都会出事**：

```
删不干净：同一份文档两次入库，一个 source 带路径、一个不带
          → 匹配不上，旧数据永远留着，检索出重复片段
误删：    两个部门各交了一份《员工手册》，source 同名
          → 一次删掉两份，而且没有任何提示
```

拆表后删除变成 `DELETE FROM documents WHERE id = <uuid>` —— 主键精确匹配，切片靠外键级联。**文件名退化成展示用的一个字段，不再是身份标识**，重名不再有影响。

> ⚠️ 一个容易误解的点：级联是**单向**的（删文档 → 删切片）。
> 拆表**不是**为了「能单独删某个切片」—— 删掉一片等于知识库里缺一块内容，
> 真要改就重新上传整份文档。级联的意义是「**删文档时不留孤儿数据**」。

### 2.2 为什么用 UUID 而不是自增 int

`documents.id` 会暴露给前端、出现在 URL 里（`DELETE /api/documents/{id}`）。

自增 ID 的两个问题：**可枚举**（用户改个数字就能探到别人的文档）、**泄露规模**（ID 大小暴露「系统里有多少文档」）。UUID 不可枚举。

切片表仍然用自增 `BIGSERIAL` —— 它不暴露给用户，只做内部主键，自增的性能和存储都更好。**ID 类型按「是否对外暴露」来选**。

### 2.3 状态机：processing → ready / failed

上传是异步的。用户在 T+0 收到响应那一刻，活才干了个零头：

| 已完成 | 还没做 |
|---|---|
| 文件收到了、存下来了 | 解析（PDF → 文字） |
| 数据库里建了一行记录 | 切片、向量化、切片落库 |

所以要有 `status` 让前端知道「到哪了」：

- **`processing`** —— 起点。**此时这份文档检索不到**
- **`ready`** —— 终态。切片全部入库，**现在才真正能被检索**
- **`failed`** —— 终态。中途出错，`error_message` 记原因

一条时间线（20 页 PDF）：

```
T+0ms     收到文件 → INSERT documents(status='processing') → 立刻返回
T+50ms    后台：pypdf 解析 → 12000 字
T+120ms   split_text → 40 个切片
T+8s      embed_texts(40 片) → 40 个向量
T+8.2s    INSERT 40 行 chunks + UPDATE status='ready', chunk_count=40（同一事务）
```

**两个实现上必须做对的地方**：

1. **`error_message` 不能省** —— 只置 failed 不记原因，这个状态就只是个开天窗的标记

2. **「全有或全无」—— `ready` 与 chunks 必须在同一个事务里提交**

   **目标**：库里永远不存在「切片写了一半」或「切片写了但状态还是 processing」的中间态。

   **手段**：40 片的向量在**事务之外**一次算完 → 进事务后只 `session.add()`
   （只进内存队列，一条 SQL 都不发）→ 最后一次 `commit()` 把 40 条 INSERT + 状态更新一起提交。
   中途任何异常 → 整个事务丢弃，数据库里**0 行**
   （注意：不是「写进去再删掉」，是**压根没写** —— `add()` 相当于 `git add`，`commit()` 才相当于 `git commit`）。

   **代价（要主动交代）**：失败时已算完的向量会白算一次。
   这是刻意的取舍 —— 白算是一次性的、可重试的、有日志的；
   留半截数据是持续性的，而且不一定看得出来（见 §7 坑 8 那 10 条孤儿切片）。

### 2.4 为什么上传必须异步（面试会把这条追问到底）

三条坏处，按严重程度排：

**① 请求挂住 → 超时 → 重复入库。**
Nginx/网关默认超时常见 60s，本地模型算几十次 embedding 十几秒起。用户看到「转圈很久然后报错」，而**报错时数据其实已经入库成功了** → 用户重试 → 同一份文档入库两遍 → 检索出重复片段。

**② 占满线程池，连累其他请求。**
embedding 是 CPU 密集任务，`asyncio.to_thread` 把它丢到线程池（默认 `min(32, CPU+4)` 个）。同时来 3-4 个上传，每个占一个线程跑十几秒，**线程池会被吃光**，连正常的检索请求都排不上队。这才是「卡顿」的真实机制：不是单个请求慢，是它把资源占住、让别的请求也慢。

**③ 用户体验上的「卡死」** —— ①② 的表象。

> 边界说明：onnxruntime 的推理跑在 C++ 层，**是否释放 GIL 没有实测过**，
> 所以这里不把 GIL 当论据。结论只依赖「线程池容量有限」，它独立成立。

### 2.5 接口返回的是起点状态，不是结果状态

`POST /api/documents` 返回 **202 Accepted** + `{id, status: "processing"}`：

- **200** 的含义是「请求已经做完了」；这里只是**被接受**了 —— 用 202 才准确
- 响应体是在 `background.add_task(...)` **派发之前**构造的，那一刻后台任务还没跑
- 所以响应里的 status 永远是 `processing`。**这不是 bug，是 PRD 要的语义**

要拿最终状态，得再发一次 `GET /api/documents`（前端就是靠这个轮询）。

### 2.6 两条失败路径分开处理

| 失败类型 | 在哪拦 | 返回什么 | 为什么 |
|---|---|---|---|
| 扩展名不支持 / 文件太大 / 空文件 | **路由层**（`check_upload`） | `400` | 这是**用户输入问题**，没必要先建一条记录再让它变 failed |
| 坏 PDF / 无文字层 / 编码失败 | **后台任务** | 落 `failed` + `error_message` | PRD F4 验收明确要求「失败文档有 failed 状态」—— 要是路由层就抛掉，文档还没「出生」，没有记录可标记 |

这解释了 `process_upload` 里为什么**解析也放在后台**。

### 2.7 解析器：为什么中间要有「段落」这一层

`parse_file` 返回的不是一整段字符串，而是 `list[ParsedSegment]`：

```python
@dataclass
class ParsedSegment:
    text: str
    page_ref: str | None = None   # "p.3"；无页概念时为 None
```

理由：**切片时需要知道「这段文字来自原文件的哪里」**。PDF 有天然页边界 → 每页一个段落带页码；md/txt/docx 没有页概念 → 整篇一个段落，`page_ref=None`。

页码必须在**切片那一刻焊上去**（`_split_segments` 里做的）。如果等入完库再回头找「这片来自第几页」，就只能去原文做字符串搜索，既慢又不可靠。

**解析失败要报错，不能静默返回空列表**：

```python
segments = await asyncio.to_thread(parser, data)
if not segments:
    raise ValueError("未从文件中提取到任何文本（可能是扫描件/纯图片文件）")
```

静默返回空 → 文档看着是 `ready` 却检索不到任何内容。**这种「沉默的失败」比直接报错难查得多**（D9 第 8 题讲过同一类问题）。

**`_decode` 要试两种编码**：中文用户的 txt 大量来自 Windows 记事本，默认 GBK。只试 utf-8 会直接抛 `UnicodeDecodeError`。

### 2.8 检索侧只认 ready

`search()` 加了 `WHERE documents.status = 'ready'`。

不过滤会怎样：processing 文档的切片可能正在写入（半截），failed 文档的切片本就不该存在 —— 都可能被检索到，喂给 LLM 后答出一个「看起来合理但基于残缺材料」的答案。

> 这条在 D9 是纯理论（那时根本没有 status 字段），D12 才第一次真正可验证（V11 用例）。

---

## 3. 代码逐行解读

### 3.1 `app/models/document.py`（重写）

```python
class DocumentStatus:
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
```

**为什么用普通类装字符串常量，而不是 Enum**：

- 数据库侧用 `String`：加一个新状态不需要 `ALTER TYPE`（PostgreSQL 改 enum 很麻烦）
- Python 侧用常量类：能写 `DocumentStatus.READY` 而不是散落的 `"ready"` 字面量，拼错时 IDE 会提示，也省掉 Enum ↔ 字符串的来回转换

```python
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
```

- `Uuid` 是 SQLAlchemy 2.0 的类型，对应 PostgreSQL 的 `uuid` 列
- Python 侧拿到的是 `uuid.UUID` 对象（不是字符串），pydantic 序列化时自动转成字符串给前端
- `default=uuid.uuid4` 传的是**函数本身**，不是 `uuid.uuid4()` —— 传后者会让所有行拿到同一个 ID（D9 讲过的「默认值在定义时求值」的同一个坑）

```python
    status: Mapped[str] = mapped_column(
        String(16), default=DocumentStatus.PROCESSING,
        server_default=DocumentStatus.PROCESSING, index=True,
    )
```

- `default` 是 **Python 侧**默认值（ORM 插入时补上），`server_default` 是 **数据库侧**默认值（DDL 里的 `DEFAULT`）。两个都写，绕过 ORM 直接写 SQL 时也不会漏
- `index=True`：列表接口和检索都会按 status 过滤

**没有配 `relationship`** —— 级联交给数据库的 `ON DELETE CASCADE` 做。删文档就是一条 `DELETE FROM documents WHERE id=...`，PostgreSQL 自己把切片带走。ORM 层不需要知道这件事，少一层隐式行为。

### 3.2 `app/models/document_chunk.py`（新建）

```python
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
```

- `ondelete="CASCADE"` 是**数据库层**的约束，写在 DDL 里
- 注意它和 SQLAlchemy 的 `cascade="all, delete-orphan"`（ORM 层）不是一回事。ORM 层的级联需要 SQLAlchemy 先把子对象加载进内存再逐个删 —— 数据量大时是灾难。**能交给数据库的就交给数据库**

```python
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
```

保留切片在文档内的序号。展示切片列表、排查「哪一片检索不准」时都要用它。

### 3.3 `app/services/document_parser.py`（新建）

```python
def check_upload(filename: str, data: bytes) -> str:
    extension = PurePosixPath(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        ...
    if not data:
        raise ValueError("文件内容为空")
    if len(data) > MAX_FILE_SIZE:
        ...
    return extension.lstrip(".")
```

- `PurePosixPath` 比 `filename.split(".")[-1]` 可靠：能正确处理 `a.tar.gz`、无扩展名、大小写等边界
- `.lower()` 让 `A.PDF` 也能通过
- 这三条检查**都不需要解析内容**，所以能放在路由层做（便宜）

```python
async def parse_file(filename: str, data: bytes) -> list[ParsedSegment]:
    parser = _PARSERS.get(extension)
    ...
    segments = await asyncio.to_thread(parser, data)
```

- `_PARSERS` 是「扩展名 → 解析函数」的字典，用**查表**代替 if/elif 链
- `asyncio.to_thread` 把同步阻塞的解析丢到线程池 —— 和 D9 的 embedding 同源手法

```python
def _parse_pdf(data: bytes) -> list[ParsedSegment]:
    reader = PdfReader(io.BytesIO(data))
    for page_no, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            segments.append(ParsedSegment(text=text, page_ref=f"p.{page_no}"))
```

- 用 `io.BytesIO(data)` 把内存里的字节包成「类文件对象」—— pypdf 要的是 file-like，不是 bytes
- `page.extract_text() or ""`：某些页提取不出文字会返回 `None`，不兜住会在 `.strip()` 上炸
- **跳过空白页**：不跳过就会产生「内容为空」的切片，白算一次 embedding

```python
def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法解码文本内容（已尝试 utf-8 / gbk）")
```

`for...try/except...continue` 这种「逐个试」的写法比较少见，但这里最直接。兜底抛错而不是返回空串，理由同 2.7。

### 3.4 `app/services/document_service.py`（新建，今日核心）

**三个函数的分工**：

```
create_document()      建记录（processing）→ 返回 id           ← 快，请求内同步调用
process_upload()       后台任务入口：解析 + 调下面那个           ← HTTP 路径
_ingest_segments()     切片 → embed → 写 chunks → 置 ready      ← 两条路径共用
```

```python
async def process_upload(document_id, filename, data) -> int:
    try:
        segments = await parse_file(filename, data)
    except Exception as e:
        logger.warning("文档解析失败 id=%s filename=%s 原因=%s", document_id, filename, e)
        await _mark_failed(document_id, f"解析失败：{e}")
        return 0
    return await _ingest_segments(document_id, segments)
```

**解析失败只 warning、不 exception**：坏 PDF 是**预期内的用户输入问题**，不是系统故障。用 `warning` 记一句 + 让文档落成 failed，没必要打一整页堆栈。

```python
async def _ingest_segments(document_id, segments) -> int:
    try:
        pairs = _split_segments(segments)
        if not pairs:
            raise ValueError("切片结果为空（原文可能只有空白字符）")
        vectors = await embed_texts([text for text, _ in pairs])

        async with async_session_factory() as session:
            for index, ((text, page_ref), vec) in enumerate(zip(pairs, vectors, strict=True)):
                session.add(DocumentChunk(...))
            document = await session.get(Document, document_id)
            document.status = DocumentStatus.READY
            document.chunk_count = len(pairs)
            document.error_message = None
            await session.commit()          # ← 切片与状态一起生效
        ...
    except Exception as e:
        logger.exception("文档入库失败 id=%s 原因=%s", document_id, e)
        await _mark_failed(document_id, str(e))
        return 0
```

- `zip(..., strict=True)` —— D9 学过的顺序保护，这里同样适用
- **一个 session、一次 commit**：第 35 片报错时，前 34 片全在同一个未提交的事务里，随异常一起回滚。这就是「要么全成、要么全无」的实现方式
- `document.error_message = None`：重跑成功要把上一次的失败原因清掉，否则列表里会出现「ready 但带着错误信息」的诡异状态
- `except` 里用 `logger.exception`（带堆栈）而不是 `warning` —— 入库失败属于**真正的异常**，需要完整现场

```python
async def _mark_failed(document_id: uuid.UUID, reason: str) -> None:
    async with async_session_factory() as session:
        ...
        document.error_message = reason[:_MAX_ERROR_LEN]
```

- **用新的 session**：调用点刚经历过异常，原来的 session 可能已处于不可用状态，复用它提交很可能再炸一次
- `[:500]` 截断：错误堆栈可能很长，别把列撑爆

```python
def _split_segments(segments) -> list[tuple[str, str | None]]:
    pairs = []
    for segment in segments:
        for chunk in split_text(segment.text):
            pairs.append((chunk, segment.page_ref))
    return pairs
```

双层循环，外层是「页」，内层是「页内的切片」。**页码在这里被复制到该页的每一个切片上**。

```python
async def delete_document(document_id: uuid.UUID) -> bool:
    async with async_session_factory() as session:
        result = await session.execute(delete(Document).where(Document.id == document_id))
        await session.commit()
        deleted = (result.rowcount or 0) > 0
    return deleted
```

**只有一条 DELETE**——切片由数据库级联。`rowcount` 判断「到底删掉了没有」，路由层用它决定返回 204 还是 404。

```python
async def ingest_texts(texts: list[str], filename: str, file_type: str = "txt"):
    document_id = await create_document(filename=filename, file_type=file_type)
    segments = [ParsedSegment(text=text) for text in texts]
    count = await _ingest_segments(document_id, segments)
    if count == 0:
        raise RuntimeError(f"文档入库失败：{filename}（详情见日志与 documents.error_message）")
    return document_id, count
```

**同步入库版**，给种子脚本／验证脚本用。和 HTTP 路径**共用 `_ingest_segments`**，切片/向量化/状态流转的逻辑只有一份。

HTTP 路径与脚本路径的差别只在于「有没有响应时限」：一个必须立刻返回、一个可以 await 到底。

### 3.5 `app/services/retrieval_service.py`（改写）

变化一：**入库相关的 `add_documents` / `clear_documents` 全部移走**。本文件只管读（`search`）+ `split_text`（写路径也复用它）。职责按读写分开。

变化二：检索 SQL 从单表变成 JOIN：

```python
    distance = DocumentChunk.embedding.cosine_distance(query_vec)
    stmt = (
        select(DocumentChunk.content, DocumentChunk.page_ref,
               Document.filename, distance.label("distance"))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.status == DocumentStatus.READY)   # ← 只检索已就绪的文档
        .order_by(distance)
        .limit(top_k)
    )
```

```python
    results = [
        {
            "content": row.content,
            "source": row.filename,       # ← 来源名改从 documents 表取
            "page_ref": row.page_ref,     # ← D12 新增
            "similarity": round(1 - row.distance, 4),
        }
        for row in rows
    ]
```

**键名仍叫 `source`**：工具层的格式化、`app/core/citation.py` 的收集器都按这个名字取来源展示名。改名会连带改三处，而它表达的语义没变 —— 没必要动。

### 3.6 `app/api/documents.py`（新建）

```python
@router.post("/documents", response_model=DocumentOut,
             status_code=status.HTTP_202_ACCEPTED)
async def upload_document(background: BackgroundTasks, file: UploadFile = File(...)):
    filename = file.filename or "untitled"
    data = await file.read()

    try:
        file_type = check_upload(filename, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    document_id = await document_service.create_document(filename=filename, file_type=file_type)
    background.add_task(document_service.process_upload, document_id, filename, data)
    ...
```

- `file: UploadFile = File(...)` —— FastAPI 看到 `UploadFile` 就按 multipart 解析（需要 `python-multipart`，今天的三个新依赖之一）
- `await file.read()` 把整个文件读进内存。大文件场景应该流式落盘/对象存储，20MB 上限下这样够用
- `raise ... from e`：保留原始异常链，排查时能看到「ValueError 导致 HTTPException」
- `background.add_task(函数, 参数1, 参数2)` —— **只传函数和参数，不要加括号调用**
- **临时方案说明**：`BackgroundTasks` 跑在同一个进程里，进程重启任务就没了，文档会永远卡在 processing。可接受的取舍 —— 要的是链路完整，不是生产级任务队列（阶段 2 再上 Celery/ARQ）

```python
    document = await document_service.get_document(document_id)
    return DocumentOut.model_validate(document)
```

`model_validate` 配合 schema 上的 `from_attributes=True`，才能直接吃 ORM 对象。没有这个配置，pydantic 会要求传 dict。

```python
@router.delete("/documents/{document_id}", status_code=204, response_class=Response)
async def delete_document(document_id: UUID) -> Response:
    deleted = await document_service.delete_document(document_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="文档不存在")
    return Response(status_code=204)
```

- 路径参数声明成 `UUID` 而不是 `str`：格式不对时 FastAPI 直接 422，不用手写解析
- 204 没有响应体，显式 `response_class=Response` 避免 FastAPI 去序列化 `None`

### 3.7 `app/schemas/document.py`（新建）

```python
class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    ...
```

`from_attributes=True` 是 pydantic v2 的写法（v1 叫 `orm_mode`）。省掉一层手写的 `document → dict` 转换。

**`id: UUID` 而不是 `str`**：pydantic 会把它序列化成字符串给前端，不用手动 `str()`。

### 3.8 引用链路带上页码（三个小改动）

```python
# app/core/citation.py —— 收集器里多存一个字段
"page_ref": item.get("page_ref"),

# app/schemas/chat.py —— 对外结构多一个字段
page_ref: str | None = Field(default=None, ...)

# app/tools/retrieval.py —— 给 LLM 看的文本里带上页码
page = f" {item['page_ref']}" if item.get("page_ref") else ""
blocks.append(f"[{index}] 来源：{source}{page}（相关度 ...）\n{item['content']}")
```

LLM 现在看到的是 `[1] 来源：report.pdf p.2（相关度 0.83）`，写引用时就能写到「第几页」这一级。

---

## 4. 类比迁移表

| 今天的新东西 | 你已经会的 |
|---|---|
| `documents` / `document_chunks` 拆表 | 前端把扁平 list 按 parentId 重组成树；表结构上的「实体分层」 |
| `ON DELETE CASCADE` | 前端删父级时手动过滤子项 —— 现在是数据库替你保证，且是原子的 |
| UUID 主键 | 前端用 `nanoid`/`uuid` 生成 key，而不是数组下标（下标可枚举、会漂移） |
| `BackgroundTasks` | 前端 `setTimeout(fn, 0)` / `queueMicrotask` —— 「先返回，活儿稍后干」 |
| `status: processing/ready/failed` | 前端上传大文件时的 `uploading / done / error` 三态 |
| `202 Accepted` vs `200 OK` | 前端 `202` 表示「已受理」；`200` 表示「已经做完了」 |
| `from_attributes=True` | TS 里手写 `toDTO(doc)` 映射函数 —— 现在是声明式的 |
| `asyncio.to_thread` | 前端把重计算丢给 Web Worker，别卡住主线程 |

---

## 5. 面试考点

### Q1：你们的知识库表怎么设计的？

> 两级：`documents` 存文档级信息（文件名/类型/状态/切片数），`document_chunks` 存切片和向量，靠外键一对多 + `ON DELETE CASCADE`。
>
> 拆表的原因有三个：一是生命周期不同，文档有 processing→ready/failed 的状态流转，切片没有状态、随文档生死；二是**字段归属**，`chunk_count` 是聚合量（「这份文档切了几片」），切片表一行 = 一个切片，没有「整体」这个视角去承载它 —— 硬塞进去就得每行都存一遍 `40`、改一次动 N 行，等于把冗余又挖一遍；三是删除语义，单表时只能靠文件名做字符串匹配删除，既会漏删（名字变了）也会误删（两个同名文档），拆表后是主键精确匹配 + 数据库级联。

### Q2：为什么上传接口要异步？不能直接等吗？

> 不等。三条理由：
> ① 一份 PDF 要算几十次 embedding，十几秒起，请求挂住会撞网关超时；更糟的是**超时那一刻数据其实已经入库成功了**，用户重试就入库两遍、检索出重复片段。
> ② embedding 是 CPU 密集任务，会占满线程池，把正常的检索请求也拖慢。
> ③ 所以接口返回 202 + 一个 processing 状态的记录，前端轮询查进展。
>
> 需要坦白的一点：现在用的是 FastAPI 的 `BackgroundTasks`，跑在同一进程里，**进程重启任务就丢了**，文档会卡在 processing。这是当前的已知取舍，生产级要上 Celery/ARQ。

### Q3：为什么用 UUID 而不是自增 ID？

> `documents.id` 要暴露给前端、出现在 URL 里。自增 ID 可枚举（改个数字就能探到别人的文档），还泄露系统规模。切片表仍然用自增，因为它不对外暴露 —— **ID 类型按「是否对外暴露」选**。

### Q4：入库失败怎么保证数据不脏？

> 保证「不脏」要三层，缺一层都不行：
>
> **① 原子性 —— 切片和 `ready` 状态在同一次 `commit()` 里落地。**
> 40 片的向量在**事务之外**一次算完 → 进事务后只 `session.add()`（只进内存队列，一条 SQL 都不发）→ 最后一次 `commit()` 把全部切片 INSERT 和 `status='ready'`、`chunk_count` 的 UPDATE 一起提交。中途任何异常 → 整个事务丢弃，切片表 **0 行**。注意不是「写进去再删掉」，是**压根没写**（`add()` ≈ `git add`，`commit()` ≈ `git commit`）；库里永远不会出现「切片写了一半」或「切片写了但状态还是 processing」的中间态。
> 代价主动交代：失败时已算完的向量白算一次 —— 白算是一次性、可重试、有日志的；留半截数据是持续性的、还不一定看得出来。
>
> **② 约束兜底 —— 删除干净靠数据库的外键 `ON DELETE CASCADE`，不靠应用代码自觉。**
> 上面那条只保证「这一次入库」干净，保证不了「删除」干净：靠应用层手动删切片，漏一个调用点就留下孤儿数据。交给数据库，删文档时它在一个事务里连坐删掉所有切片。
> ——这个约束真出过事：模型里写对了，库里却没建出来（`create_all` 见表已存在就整张跳过），静默积了 10 条孤儿切片。**ORM 模型写对 ≠ 库里建出来了。**
>
> **③ 失败可见 —— 另起一个 session 标 `failed` + 写 `error_message`。**
> 用新 session 是因为原 session 刚经历过异常，可能已不可用。目的是让用户看得见这份传失败了，而不是留一条永远卡在 processing 的僵尸记录。
>
> 追问预备：如果问「那第 35 片报错呢」—— 写库阶段（`add()`）几乎不会失败；真正容易失败的是前面那次 `embed_texts`，而那时**事务还没开**，自然什么都不会留下。
>
> 另外解析失败和入库失败都只影响这一份文档，不会中断服务。

### Q5：引用能精确到页码吗？

> 能。PDF 解析时按页产出段落、带上 `page_ref="p.3"`，切片那一刻页码就复制到每一片上，存进 `document_chunks.page_ref`。
> 检索结果带上它，工具格式化成 `[1] 来源：xxx.pdf p.3`，LLM 就能写到页这一级；结构化那边 `sources[].page_ref` 给前端做跳转。
> 这也是为什么页码必须在**切片时**焊上去 —— 事后回头找「这片来自第几页」只能靠字符串搜索，既慢又不可靠。

---

## 6. 自测题（先自己做，再看参考答案）

1. **现在 `documents` 表和拆表前比，一行的含义有什么不同？** 拆表解决的三个问题分别是什么？
2. **删除文档时，切片是怎么被删掉的？** 为什么不推荐在 ORM 里配 `cascade="all, delete-orphan"`？
3. **`chunk_count` 为什么不能放进切片表？**
4. **上传接口为什么返回 202 而不是 200？返回的 status 为什么永远是 processing？**
5. **上传为什么必须异步？说出两条理由，并说明「超时」为什么比「慢」更糟。**
6. **解析失败（坏 PDF）和扩展名不支持，分别在哪一层拦？为什么不一样？**
7. **为什么 `page_ref` 必须在切片那一刻写进去，而不是入库后再回填？**
8. **`_mark_failed` 为什么要用一个新 session？**
9. **检索 SQL 里的 `WHERE documents.status = 'ready'` 去掉会怎样？**
10. **（加试）`SELECT` 出来的切片要带上文件名，为什么用 JOIN 而不是把 `filename` 冗余到切片表里？**

### 参考答案要点

1. 拆表前一行 = 一个切片；拆表后一行 = 一份文档。三个问题：字段归属错位（filename 存 N 遍）、聚合量无处安放（chunk_count）、删除靠字符串匹配（漏删 + 误删）。
2. 靠外键上的 `ON DELETE CASCADE`，数据库在一个事务里完成。ORM 的 `cascade="all, delete-orphan"` 需要先把子对象加载进内存再逐个删，切片多时是灾难；能交给数据库的就交给数据库。
3. 它是**聚合量**（这份文档切了几片），切片表的一行没有「整体」这个视角；而且它需要被回写（`UPDATE`），只能挂在文档级。
4. 200 = 「已经做完了」，202 = 「已受理，处理未完成」。响应体是在派发后台任务**之前**构造的，那一刻后台还没跑，所以永远是 processing。最终状态靠 `GET /api/documents` 轮询。
5. ① 请求挂住会撞网关超时，而**超时那一刻数据其实已经入库成功了** —— 用户重试就入库两遍、检索出重复片段；「慢」只是体验差，超时会造成**数据重复**。② embedding 是 CPU 密集任务，占满线程池会把正常检索请求一起拖慢。
6. 扩展名/大小 → 路由层 400（用户输入问题，不必建记录）；坏 PDF → 后台任务落 failed（PRD F4 验收要求「失败文档有 failed 状态」，而且在路由层抛错时文档还没出生，没有记录可标记）。
7. 切片那一刻才知道「这段文字属于哪一页」，顺手复制上去，零成本。事后回填只能拿切片内容去原文做字符串搜索 —— 既慢又不能保证唯一匹配。
8. 调用点刚经历过异常，原 session 可能已不可用，复用它提交很可能再炸一次。
9. processing 文档的切片可能只写了一半，failed 文档的切片本就不该存在。不过滤就可能命中残缺内容，喂给 LLM 后答出一个「看起来合理但基于残缺材料」的答案。
10. 拆表的初衷就是「字段各归其位」。把 `filename` 冗余回切片表等于把坑 1 再挖一遍：改名/更新时要同步 N 行，还会出现不一致。JOIN 的成本在这个数据量下可以忽略，一致性却是白拿的。

---

## 7. 踩坑记录

### 坑 1：旧表把「切片」和「文档」混在一行

**现象**：`chunk_count` 这类聚合量无处安放；删除靠 `WHERE source=...` 匹配字符串。
**根因**：实体层级错位 —— 表名叫 `documents`，一行装的却是一个切片。
**修法**：拆成 `documents` + `document_chunks`，级联交给数据库。

### 坑 2：TestClient 与数据库操作抢事件循环

**现象**：

```
RuntimeError: Task ... got Future ... attached to a different loop
```

**根因**：`fastapi.testclient.TestClient` 会在**独立线程里起一个自己的事件循环**，而验证脚本还要直接 `await` 数据库操作（查切片数、手动改状态）。两个事件循环抢同一个连接池。
**修法**：改用 `httpx.ASGITransport`，让 HTTP 调用和数据库操作跑在**同一个 loop** 里。

```python
transport = ASGITransport(app=app)
async with AsyncClient(transport=transport, base_url="http://testserver") as client:
    ...
```

（如果确实要用 TestClient，就得在用它之前 `await engine.dispose()` 把连接池清空，别让旧 loop 的连接被新 loop 复用 —— 见坑 4 的处理方式。）

### 坑 3：上传响应里的 status 永远是 processing，这不是 bug

**现象**：V1-V5 第一次跑，我断言「上传成功 → status=ready」，结果全红。

**根因**：响应体是在 `background.add_task(...)` **派发之前**构造的，那一刻后台任务还没开始跑。最终状态要靠后续 `GET /api/documents` 才能看到 —— 这正是 PRD 要的语义（接口返回**起点状态**）。

**修法**：把断言拆成两件事 ——
- **接口契约**：202 + status=processing + chunk_count=0（V1-V5）
- **状态流转**：列表里最终变成 ready / failed（V9）

两件事分开验，才不会把「接口语义」和「状态流转」搅在一起。

### 坑 4（本来就存在，回归时暴露）：`d11_http_verify.py` 从不播种知识库

**现象**：单独跑或换顺序跑，H1 报错（期望回答含「星河」，实际不含）。
**根因**：这个脚本默默依赖「上一个脚本跑完留下的数据」。回归时按 d9→d11→d10→d10→d11_http 的顺序跑，轮到它时知识库里只剩 `d10-e2e` 的内容。
**修法**：加 `prepare_kb()`，自己清库 + 重新入库。**每个验证脚本都该能独立跑通**。

同时踩到坑 2 的变体：`asyncio.run(prepare_kb())` 之后必须 `await engine.dispose()`，否则连接池里残留的连接绑定在已关闭的 loop 上，TestClient 的 loop 复用就炸。

### 坑 5（本来就存在，回归时暴露）：`d10_agent_rag_verify.py` 在 D11 之后其实已经坏了

**现象**：`answer = await run_agent(...)` 之后 `keyword in answer` —— 而 D11 把 `run_agent` 的返回值从 `str` 改成了 `ChatResult`。

**根因**：D11 改返回结构时确认了「调用方只有验证脚本」，但没有真的去改这些脚本。这是个**静默破损**：脚本不会报语法错，只在运行时行为异常。

**修法**：`result = await run_agent(...)`，再用 `result.answer`。

**教训**：改了函数返回类型，**立刻把调用点全部改掉**，别相信「只有一个调用方，以后再说」。这次是回归测试把它挖出来的。

### 坑 6：`_decode` 只试 utf-8 会让 GBK 文件直接失败

中文用户的 txt 大量来自 Windows 记事本，默认 GBK。只试 utf-8 会抛 `UnicodeDecodeError` —— 而且是那种「用户觉得文件明明没问题」的失败。逐个试编码，兜底抛明确错误。

### 坑 7（最严重的一条）：`.gitignore` 把源码目录一起吞了

**现象**：`git status` 里看不到 `app/models/document.py`，也看不到新写的 `app/models/document_chunk.py`。

**根因**：D9 为了忽略 fastembed 下载的模型缓存（90MB+），在 `.gitignore` 里加了一行：

```gitignore
models/          # ← 没有前导斜杠
```

`.gitignore` 里**不带前导 `/` 的模式会匹配任意层级**。这一行不只忽略了根目录的 `models/`，还把 `app/models/` 一起忽略了。

**更糟的是时间点**：`app/models/document.py` 恰好是 D9 创建的文件，而这条规则也在 D9 同一次提交里加上 —— **它当场就被自己的规则吞掉了，从没进过 git**。

```bash
$ git log --oneline -- app/models/document.py
(空 —— 从未提交)

$ git ls-files app/models/
app/models/__init__.py
app/models/message.py     ← D3 创建，当时还没有这条规则，所以被正常提交了
app/models/session.py
```

**后果**：这个仓库 clone 到别的机器上，`app/services/retrieval_service.py` 一 import 就报 `ModuleNotFoundError`。而本地因为文件一直在磁盘上，**跑得一切正常** —— 典型的「只在别人机器上才会炸」的坑。

**修法**：加前导斜杠锚定到仓库根目录。

```gitignore
# ⚠️ 必须锚定到根目录：写 `models/` 会匹配任意层级的 models 目录，把 app/models/ 一起吞掉
/models/
```

**教训**：`.gitignore` 里的目录名模式**默认是「任意层级」**。忽略某个特定目录时一律加前导 `/`；改完 `.gitignore` 用 `git status --ignored` 扫一眼，确认没有顺手吞掉源码目录。这类问题不会报错，只会安静地把文件挡在版本控制之外。

### 坑 8（2026-09-18 收尾后暴露；性质：数据永久泄漏）：表根本**没有外键**，级联删除从未生效

**现象**：重跑验证脚本，V1-V11 全过，V12-V13 报红。而且失败得很有迷惑性 —— 删除接口是成功的：

```
删除前：文档 ac3c5e52-... 有 1 个切片
[✅] DELETE → HTTP=204
[❌] 切片被级联删除：1 → 1        ← 文档没了，切片还在
[✅] 列表里已不含该文档
```

**第一反应一定是怀疑代码，但代码是对的**：`document_chunk.py:44` 明明写着

```python
ForeignKey("documents.id", ondelete="CASCADE")
```

**根因在数据库，不在模型。** 查 `pg_constraint`：

```sql
SELECT conname, contype, pg_get_constraintdef(oid)
FROM pg_constraint WHERE conrelid = 'document_chunks'::regclass;
```

```
document_chunks_pkey  type=b'p'  PRIMARY KEY (id)     ← 只有主键，零外键
```

**ORM 模型写对了，不代表库里建出来了。** 数据库不知道「切片的爸爸是谁」，删文档自然不连坐。

**它是怎么坏掉的**（这段最有价值）：

旧版 `d12_migrate.py` 的 drop 只写了一张表：

```python
await conn.execute(text("DROP TABLE IF EXISTS documents CASCADE"))
await conn.run_sync(Base.metadata.create_all)      # checkfirst 默认 True
```

三步形成死循环：

1. 某个时刻 `document_chunks` 处于「无外键」状态（D12 开发早期表先建、外键后加进模型，很常见的顺序）
2. 跑 migrate：`DROP TABLE documents CASCADE` 想连带清掉依赖对象 —— **但"没有外键"的表算不上依赖对象**，chunks 表活了下来
3. `create_all` 看到表已存在 → **`checkfirst=True` 直接整张跳过**，不会补约束

于是**再跑一百次 migrate 也修不好**：每次都是「删 documents、重建 documents、放过坏掉的 chunks」。

**后果（比 V12 报红严重得多）**：
- **删文档永久泄漏切片** —— 看起来删干净了，其实只删了半份
- **`delete_all_documents()` 同样清不干净**，而它是「换 embedding 模型后全量 reindex」的前置步骤 → 旧向量永远清不掉，新旧向量混在一张表里，检索排序直接乱掉（`embedding_service.py` 开头那段注释讲的物理约束）
- 库里已经躺了 **10 条孤儿切片**。它们检索不到（`search` 是 `JOIN documents ... WHERE status='ready'`，JOIN 不上）→ **不污染答案，但属于「看不见的存储泄漏」**

这正是 §2.3 第 2 条「全有或全无」要防的局面 —— 只不过制造这些残留的不是"失败"，而是"约束没生效"。

**修法**（两处，都在 `d12_migrate.py`）：

① drop 按依赖顺序**显式列出两张表**，不依赖「前任的外键状态」：

```python
await conn.execute(text("DROP TABLE IF EXISTS document_chunks CASCADE"))   # 先子表
await conn.execute(text("DROP TABLE IF EXISTS documents CASCADE"))         # 再父表
```

② 建完表**立刻自查结构**，不自查就是又一次静默失败：

```python
fk_defs = [...从 pg_constraint 查 document_chunks 的外键定义...]
if not [d for d in fk_defs if "ON DELETE CASCADE" in d]:
    raise RuntimeError("结构自检失败：document_chunks 没有带 ON DELETE CASCADE 的外键，删文档会留下孤儿切片")
```

抛异常 → `engine.begin()` 的整个事务回滚，库里不会留下半成品结构。

修复后：

```
✅ 结构自检通过： FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
[✅] 切片被级联删除：1 → 0
孤儿切片：10 → 0
```

**教训（三条）**：
1. **`create_all` 不是迁移工具**。默认 `checkfirst=True` —— **表存在就整张跳过**：不补外键、不加列、不改类型、不建索引。模型改了它一声不吭。**改结构只能靠 Alembic（保数据）或「显式 drop + 重建」（练习期）**。
2. **`DROP ... CASCADE` 连带不了"坏掉的那张表"**。它只连带有外键关系的对象，而「坏掉的那张表恰好就是没有外键的表」—— 这是个完美的悖论式陷阱。重建表时，**把要 drop 的表一张张写出来**。
3. **排查「删了却没删干净」「约束没生效」这类问题时，先查数据库实际结构**（`pg_constraint` / `information_schema`），不要对着 ORM 模型猜。**模型写对了，不等于库里建出来了。**

**这个坑和坑 7 是同一类**：`.gitignore` 的 `models/` 你以为只匹配根目录，`create_all` 你以为会把模型改动同步进库 —— 都是**「你以为这个操作是精确的、幂等的，实际它的判断标准不是你以为的那个」**。这类坑的共同点是**不报错**，安安静静地把系统留在错误状态，直到某天以一个看似无关的断言失败暴露出来。

---

## 附录 A：端到端运行链路（实测）

### A.1 迁移

```
$ uv run python -m scripts.d12_migrate
① 删除旧的 documents 表（CASCADE 连带依赖对象）
② 确保 vector 扩展存在
③ 按新模型建表（documents + document_chunks）

✅ 迁移完成，当前表： document_chunks, documents, messages, sessions
```

**为什么直接 drop 重建**：旧表里只有验证脚本灌进去的测试数据，没有需要保留的东西。为它写一套「把散落的切片行聚合成文档行、再回填外键」的数据迁移，纯属浪费。

这是工程判断，**不是通用做法** —— 真实生产必须保数据，那时用 Alembic + 数据回填脚本一步步走。

### A.2 验证结果（`scripts/d12_documents_verify.py`，8 组全过）

```
V1-V5. 上传各种格式（接口契约：已受理）
  [✅] md                    HTTP=202 status=processing 切片=0
  [✅] txt(GBK)              HTTP=202 status=processing 切片=0
  [✅] docx                  HTTP=202 status=processing 切片=0
  [✅] pdf（两页，应带页码）      HTTP=202 status=processing 切片=0
  [✅] PDF 无文字层（应失败）     HTTP=202 status=processing 切片=0

V5b. PDF 页码（page_ref）
  切片数=2，页码=['p.1', 'p.2']
  [✅] 两页各一片且页码正确
    report.pdf  p.2  相似度=0.8274
    report.pdf  p.1  相似度=0.8198
  [✅] 页码透传到了检索结果（引用能精确到页）

V6-V7. 非法上传
  [✅] 不支持的扩展名 → 400 detail=不支持的文件类型「.exe」，仅支持 .docx / .md / .pdf / .txt
  [✅] 超大文件(21MB)  → 400 detail=文件过大（21.0MB），上限 20MB

V8. 起点状态（直接建记录，不派后台任务）
  [✅] 新建文档 status=processing chunk_count=0

V9. 文档列表
  pending.md       processing 切片=0   错误=-
  scanned.pdf      failed     切片=0   错误=解析失败：未从文件中提取到任何文本（可能是扫描件/纯图片文件）
  report.pdf       ready      切片=2   错误=-
  policy.docx      ready      切片=1   错误=-
  gbk.txt          ready      切片=1   错误=-
  formats.md       ready      切片=1   错误=-
  [✅] 四种格式都在列表里 / 失败文档为 failed / 成功文档为 ready / processing 也在列表里

V10. 上传后能被检索到
  检索「玄鸟项目是谁负责的」
    来源=policy.docx  页码=None  相似度=0.7756     ← 上传 2 秒前才进库的文档
  [✅] 命中 docx 里上传的虚构事实

V11. 只检索 ready 文档的切片
  [✅] processing 文档的切片检索不到（命中 3 条）
  [✅] 改成 ready 后立刻可检索（命中 3 条）

V12. 删除与级联
  删除前：文档 5582aeb7-... 有 1 个切片
  [✅] DELETE → HTTP=204
  [✅] 切片被级联删除：1 → 0
  [✅] 列表里已不含该文档

V13. 错误码
  [✅] 删除不存在的文档 → HTTP=404
  [✅] 非法 UUID          → HTTP=422
```

**V11 是今天最值得看的一条**：给一个还在 `processing` 的文档手工塞一个带真实向量的切片，检索**命中不到**；把状态改成 `ready`，同样的检索**立刻命中**。这是「半截数据会不会污染检索」的直接证明。

### A.3 回归（老脚本在新表结构下）

| 脚本 | 结果 |
|---|---|
| `d9_retrieval_verify` | ✅ 全部用例完成 |
| `d11_citation_verify` | ✅ A1/A2/A3/B1/B2/B3/B4 全过 |
| `d10_tool_verify` | ✅ 全部用例完成 |
| `d10_agent_rag_verify` | ✅ 三个用例全过（触发判定 + 内容判定） |
| `d11_http_verify` | ✅ H1/H2/H3/H4 全过（修复后） |

回归过程中修掉两处**本来就存在的问题**（见 §7 坑 4、坑 5）。

### A.4 本地跑一遍

```bash
cd ~/workspace/bs/AgentForge-Lite

# 迁移（只需一次）
uv run python -m scripts.d12_migrate

# 验证
uv run python -m scripts.d12_documents_verify

# 起服务，用 curl 或 /docs 试
uv run uvicorn app.main:app --reload --port 8000
```

```bash
# 另一个终端
curl -s -X POST http://localhost:8000/api/documents \
  -F "file=@/path/to/员工手册.md" | python3 -m json.tool --no-ensure-ascii

curl -s http://localhost:8000/api/documents | python3 -m json.tool --no-ensure-ascii

curl -s -X DELETE http://localhost:8000/api/documents/<id> -w "%{http_code}\n"
```

浏览器打开 `http://localhost:8000/docs` → `POST /api/documents` 支持直接选文件上传。

---

## 附录 B：首次作答记录与批改（2026-09-20）

> **这一节是错题本，不是标准答案** —— 标准答案在 §6。
> 用法：复习时先按 §6 的题目自己答一遍，再回来看这一节，检查有没有重犯同样的毛病。
> 下面的「作答」是首次答案的原话（只修正了明显笔误）。

### 本次结果

| 判定 | 题数 | 题号 |
|---|---|---|
| ✅ 完整正确 | 2 | 4、8 |
| ⚠️ 只答了一半 | 6 | 1、2、6（漏问）、7、9、10（漏面 / 漏场景） |
| ❌ 答偏 | 1 | 3 |
| ⚠️ 其他瑕疵 | 1 | 5（因果链多了个没必要的前提） |

**核心问题不在知识，在答题动作**：流程是「抓关键词 → 回忆 → 输出」，不是「逐问核对」。详见本节末尾。

---

### 第 1 题 ⚠️ 漏了前半问

**作答原文**：
> (1) 字段归属错误。(2) 聚合量无法匹配。(3) 删除会靠字符串进行匹配，不够精准。

**判定**：三个理由方向全对。但题目问了两件事，只答了第二件。

**该补的**：
- **漏答的前半问**：一行的含义变化 —— 拆表前一行 = **一个切片**；拆表后一行 = **一份文档**。先有它，三个理由才推得出来。
- **第 3 点太笼统**：`WHERE source='员工手册.md'` 的具体风险是两头 —— **误删**（同名来源的行全部命中）和**漏删**（文件名有一点差异就匹配不上，比如被重命名过、多个空格）。

### 第 2 题 ⚠️ 只答了后半问

**作答原文**：
> ORM 层的级联需要 SQLAlchemy 先把子对象加载进内存再逐个删 —— 数据量大时是灾难。能交给数据库的就交给数据库。

**判定**：后半问（为什么不推荐 ORM cascade）答得对。

**该补的**：
- **漏答的前半问**：切片**靠外键上的 `ON DELETE CASCADE` 删**，由**数据库在同一个事务里完成**。应用层只发一条 `DELETE FROM documents`，切片的事完全不管。
- 面试时两问连起来说：「谁删的 → 数据库；为什么不自己删 → ORM 得先把子对象加载进内存再逐个删，切片多时是灾难」。

### 第 3 题 ❌ 答偏了

**作答原文**：
> 因为 chunk_count 字段代表的是，当前文档切分成了哪些片段。

**判定**：答的是「chunk_count **是什么**」，题目问的是「**为什么不能放进切片表**」。两回事。

**该补的**：
1. **它是聚合量，切片表的行没有「整体」视角** —— 切片表一行 = 一个切片。要在切片表里表达「这份文档共 40 片」，就得**每一行都存一遍 40**，等于把坑 1（字段冗余、归属错位）再挖一次。
2. **它需要被回写（`UPDATE`）** —— 切片数变化时：挂在文档级改 **1 行**；挂在切片表要改 **N 行**，还得保证 N 行一致。

### 第 4 题 ✅ 完整正确

**作答要点**：202 = 已受理（处理未完成）vs 200 = 已完成；响应体在派发后台任务**之前**构造，那一刻后台还没跑，所以永远是 `processing`；最终状态靠 `GET /api/documents` 轮询。

**判定**：三点全中，可以直接上考场。

### 第 5 题 ⚠️ 第二点的因果链多个了前提

**作答原文**：
> 超时的话，因为 embedding 操作过慢，有的时候其实已经入库了，但是报了超时错误，这样用户会以为没有入库成功，会再次请求，就会导致重复入库。又因为 embedding 是 cpu 任务，如果重复进行 cpu 运算的话，会导致线程占满，会阻塞其余任务。

**判定**：
- **第一点 ✅ 完整**：「入库其实已经成功了，但用户看到超时 → 以为没成功 → 重试 → 重复入库」，正是「超时比慢更糟」的核心。
- **第二点 ⚠️**：把「线程池被占」建在「**重复** CPU 运算」这个前提上。其实**不需要「重复」** —— 单次上传任务本身就是 CPU 密集的，它在那几秒里持续占用执行资源，这期间同进程的其他请求（尤其检索）就会被拖慢。「重复」只会让情况更坏，不是成因。

> ⚠️ **这条要谨慎使用**：`embed_texts` 走的是 `asyncio.to_thread`，占线程池里的**一个**线程；而 fastembed 底下的 ONNX Runtime 推理本身是多线程的、会吃满 CPU 核心。到底主要来自「线程占用」还是「CPU 争抢」，**本项目没有实测过**。
> 面试建议：别把「占满线程池」写死（被追问「一个任务占几个线程、池子多大」会露怯），退一步只讲「**CPU 密集任务会拖慢同进程的其他请求**」——这句绝对站得住。

### 第 6 题 ⚠️ 第二问没答

**作答原文**：
> 拓展名不支持，在路由层直接拦截 400，而解析失败，解析落文档的时候报错。

**判定**：① 分别在哪层拦 ✅；② 为什么不一样 ❌。

**该补的**：
- 扩展名/大小是**用户输入问题**，进系统前就拦掉，**不必建记录**（省掉一次注定失败的入库）。
- 坏 PDF 是**文件内容问题**，不去解析就不知道。而 PRD F4 要求「失败文档有 failed 状态」—— **如果在路由层拦，文档还没「出生」，根本没有记录可以标 failed**，用户在界面上什么都看不到。

### 第 7 题 ⚠️ 术语说偏 + 漏了致命点

**作答原文**：
> 因为只有在切片的时候才能知道文档真正属于哪一个片段，如果入库后，又要再进行反向匹配，这样不准确也浪费功夫。

**判定**：方向对（事后要反向匹配、慢）。

**该补的**：
- **术语**：`page_ref` 是「属于**哪一页**」，不是「哪个片段」。切片那一刻手里有 parse 阶段带上来的 `segment.page_ref`，顺手写到切片行上，**零成本**。
- **漏掉的致命点**：事后回填的问题**不只是「费功夫」，而是「不保证唯一匹配」** —— 拿切片内容回原文做字符串搜索，同一段文字可能在多处出现（页眉、重复段落、表格），你**无法确定该回到哪一页**。慢是次要的，可能匹配错才是要命的。

### 第 8 题 ✅ 完整正确

**作答原文**：
> 因为调用点刚经历过异常，原来的 session 可能已处于不可用状态，复用它提交很可能再炸一次。

**判定**：完整，可以直接上考场。

**加分追问**（面试官可能继续追）：**为什么原 session 会不可用？** —— 异常发生后事务通常已进入**失效 / 待回滚**状态，此时再 `commit()` 很可能再抛一次。能补上这层更稳。

### 第 9 题 ⚠️ 只答了一半场景

**作答原文**：
> 这样的话可能会把落库一半的数据返回回来，喂给 llm 后可能会造成看起来合理，但是实际缺少完整依据的回答。

**判定**：processing 场景 ✅；failed 场景 ❌。

**该补的**：
- **processing**：切片可能**只写了一半** → 命中残缺内容
- **failed**：**failed 文档的切片本就不该存在**。正常情况是 0 行，但「残留」是真实可能的（见 §7 坑 8）

**关键区别**（和坑 8 联动，务必分清）：

| 情况 | 能被检索到吗 | 原因 |
|---|---|---|
| 坑 8 那 10 条**孤儿切片**（文档行已被删） | **不能** | `JOIN documents` 接不上 → 只是存储泄漏 |
| **failed 文档的残留切片**（文档还在、status=failed） | **能** | JOIN 接得上 → 去掉过滤立刻被命中 |

所以 `WHERE documents.status = 'ready'` 的价值是**同时挡住两种东西**：正在写一半的（processing）+ 状态不对的（failed）。

### 第 10 题 ⚠️ 只答了 B 面

**作答原文**：
> 就是因为要用文件 id 进行精准匹配，所以使用 join 去首先选中对应的文章才可以，如果把 filename 再冗余到切片，那么更新等一些操作都要执行多次，失去拆表意义。

**判定**：B 面（冗余的缺点）✅；A 面（JOIN 这一侧的成本）❌。

**该补的**：题目是「为什么用 A 不用 B」，**两面都要答**：

> JOIN 不是免费的（多一次连接 + 走索引），但**在这个数据量下成本可以忽略**；
> 而冗余带来的不一致是**持续存在的维护负担**。
> 取舍是：**用可忽略的读成本，换掉持续的一致性风险**。

---

### 本次暴露的方法问题（比题目本身更重要）

**10 题里有 6 题是「只答一半」**。而且第 6 题在作答前已被明确提示过「这题有两问」，仍然漏了 —— 说明这不是粗心，是**答题动作里缺了「核对问号」这一步**。

**改法（一个 5 秒钟的动作）**：

读题后**先只写问号，不写内容**。例如第 6 题：

```
① 分别在哪层拦 →
② 为什么不一样 →
```

再逐个填。答完回头数一遍：**问号数 == 答案段数？** 对不上就是漏了。

面试里漏答一问 ≈ 白丢本该拿到的一半分；更亏的是，**你已经答对的那一半也会被忽略** —— 面试官的印象是「这个人没答全」，而不是「他答对了一半」。

---

## 附录 C：§5 面试考点（Q1–Q5）验收记录（2026-09-20）

> §6 是题、附录 B 是自测题（1-10）的作答与批改，本节是 **§5 面试五问（Q1-Q5）** 的作答与批改。
> 目的和 附录 B 一样：留错题本，不重复标准答案。

### C.1 结果总览

| 题 | 判定 | 说明 |
|---|---|---|
| Q1 表怎么设计 | ✅ 通过 | 两处用词需修正（见 C.5） |
| Q2 为什么异步 | ⚠️ 首轮漏两块 | 完整答法见 C.3 |
| Q3 为什么 UUID | ✅ 通过 | 自增/ UUID 的对照组也答对了 |
| Q4 失败怎么保证不脏 | ❌ 二轮仍不对 | 完整答法见 C.4 —— **这题的完整答案比 §5 原先写的多一层** |
| Q5 页码精度 | ✅ 通过 | 题干所问的四环节链路答对；两处待补（见 C.5） |

### C.2 首轮的一个现象：5 题里 3 题照抄原文

首轮作答时 Q1、Q4、Q5 是一字不差照抄 §5（连 markdown 格式都没改），只有 Q2、Q3 是自己的话。

**为什么这是个真问题**：检验方式是「用自己的话把机制讲出来」。答案就在同一份文档里，照抄**无法区分「真记住了」和「按了 Ctrl+C」** —— 而这五题是准备**说给面试官听**的，考场上没有文档可抄。所以 Q1/Q4/Q5 首轮判定为**无法验收**，要求重答。

> 顺带：Q4 在重答前已被明确提醒「文档答案有已知问题，别照抄」，仍照抄。**"绕开费力的那一步"和 附录 B 那个"漏答一问"是同一类动作问题。**

### C.3 Q2 完整答法（补首轮漏的两块）

> **不等。三条理由：**
>
> ① 一份 PDF 要算几十次 embedding，十几秒起，请求挂住会撞网关超时；更糟的是**超时那一刻数据其实已经入库成功了** —— 用户以为失败去重试，结果入库两遍、检索里出现重复片段。**"超时"比"慢"更危险，因为它给出的是错误的失败信号。**
>
> ② embedding 是 **CPU 密集**任务，会**拖慢同进程的其他请求**（尤其是检索）。注意这条在异步下依然存在 —— 异步只解决"不挂住那一个 HTTP 请求"，**不解决 CPU 争抢**。
> （话术提醒："占满线程池"这个因果未实测，别写死；讲"CPU 密集任务会拖慢同进程其他请求"这句一定站得住。）
>
> ③ 所以接口立刻返回 **202 Accepted** + 一条 `status=processing` 的记录，解析与向量化交给后台；前端拿 `id` **轮询** `GET /api/documents`，看状态流转到 `ready` 还是 `failed`。
>
> **主动交代局限（加分项）**：现在用的是 FastAPI 的 `BackgroundTasks`，任务跑在**同一个进程**里 ——
> - **进程重启，进行中的任务就丢了**，文档会永远卡在 `processing`；
> - 它与 CPU 密集任务同进程，**抢资源的问题并没有解决**。
>
> 生产级要换 Celery / ARQ 这类带独立 worker + 持久化队列的方案。

### C.4 Q4 完整答法（三层，缺一层都不行）

> **① 原子性 —— 切片和状态在同一次 `commit()` 里落地。**
>
> 40 片的向量在**事务之外**一次算完 → 进事务后只 `session.add()`（只进内存队列，**一条 SQL 都不发**）→ 最后一次 `commit()` 把全部切片的 INSERT 和 `status='ready'`、`chunk_count` 的 UPDATE **一起**提交。中途任何异常 → 整个事务丢弃，切片表 **0 行**。
>
> 注意这是**「压根没写」**，不是「写进去再删掉」—— `add()` ≈ `git add`，`commit()` ≈ `git commit`。所以库里不会出现「切片写了一半」或「切片写了但状态还是 processing」的中间态。
> **代价要主动说**：失败时已算完的向量白算一次。这是刻意取舍 —— 白算是一次性的、可重试的、有日志的；留半截数据是持续性的、还不一定看得出来。
>
> **② 约束兜底 —— 删除干净靠数据库的外键 `ON DELETE CASCADE`，不靠应用代码自觉。**
>
> ① 只保证**「这一次入库」**干净，保证不了**「删除」**干净：靠应用层手动删切片，**漏一个调用点就留下孤儿数据**。交给数据库，删文档时它在同一个事务里连坐删掉所有切片。
>
> 这块真踩过：ORM 模型里 `ondelete="CASCADE"` 写对了，**库里那条外键根本没建出来**（`create_all` 见表已存在就整张跳过），静默积了 10 条孤儿切片。教训：**ORM 模型写对 ≠ 库里建出来了** —— 建表后要用 `pg_constraint` 自检（见 §7 坑 8）。
>
> **③ 失败可见 —— 另起一个 session 标 `failed` + 写 `error_message`。**
>
> 用新 session 是因为**原 session 刚经历过异常，可能已处于不可用状态**，复用它提交很可能再炸一次。目的是让用户**看得见**这份传失败了，而不是留一条永远卡在 `processing` 的僵尸记录。
>
> **追问预备**：如果问「那第 35 片报错呢」—— 写库阶段（`add()`）几乎不会失败；真正容易失败的是前面那次 `embed_texts`，而那时**事务还没开**，自然什么都不会留下。（另：解析失败和入库失败都只影响这一份文档，不会中断服务。）

### C.5 Q1 / Q5 的待补点

**Q1（两处用词）**

| 原话 | 修正 |
|---|---|
| 「聚合字段**没办法在一张表格体现**」 | **说反了** —— 单表完全存得下，真代价是**冗余 N 份 + 更新要动 N 行**（切片表一行没有「整体」视角）。原始措辞来源是 §5 Q1 旧文写的「切片表放不下」，**已修** |
| 删除场景「删除不准，或者**冗余**」 | 删除场景不产生冗余，产生的是**残留**。两个风险是**漏删**（名字变了匹配不上）和**误删**（两个同名文档一起干掉） |
| 「document 删除，关联片段全部删除」 | 只讲了**结果**，没讲**机制** —— 外键 `ON DELETE CASCADE`，**由数据库在一个事务里完成**，应用层只发一条 `DELETE FROM documents` |

**Q5（两处待补）**

1. **两条输出通道要分开**：

   | 通道 | 给谁 | 形态 |
   |---|---|---|
   | A | **LLM** 读 | 文本 `[1] 来源：xxx.pdf p.3` —— 让它写答案时能标出处 |
   | B | **前端**用 | 结构化 `sources[].page_ref` —— 点击跳转靠的是**这个** |

   原答案把 B 的功能挂到 A 上了：前端拿的是 JSON 字段，不是那行文本。

2. **为什么页码必须在切片那一刻焊上去，而不是入库后回填**：
   - 回填要拿切片内容回原文做**字符串搜索** —— **慢**是次要的；
   - 更要命的是**不保证唯一匹配**：同一段文字可能出现在多处（页眉、重复段落、表格），你**无法确定该回到哪一页**。切片时手里正好有 parse 阶段带下来的 `page_ref`，写上去是**零成本**的。

### C.6 本次暴露的两个动作模式

| # | 模式 | 证据 |
|---|---|---|
| 1 | **讲结果不讲机制** | 附录 B 第 2 题「切片怎么被删的」只答了 ORM cascade 的缺点；本次 Q1「删文档关联片段全删」只答结果，未答外键机制 |
| 2 | **漏答其中一问** | 附录 B 的 1、2、6 题（题干里确有第二个问号）。改法见 附录 B 末尾的「先列问号」动作 |

> **界定提醒**：判定「漏问」必须**对照题干原文的问号数**。本次曾把 Q5 的补充要求（「为什么不能回填」）误判为「漏了一整问」，实际 Q5 题干只有一问 —— 那条是重答提示里的附加要求，不算题目的一问。
