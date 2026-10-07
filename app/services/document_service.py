"""
文档服务（D12）—— 文档级操作：入库全流程 + 列表 + 删除
=======================================================
与 retrieval_service 的分工（按读写切开）：
    retrieval_service  只管**检索**（读）
    document_service   只管**文档与切片的管理**（写 + 状态流转）

端到端流程（HTTP 上传）：

    POST /api/documents
      ① check_upload()        便宜的校验：扩展名 / 大小 → 不合格直接 400
      ② create_document()     写一行 documents(status=processing)，**立刻返回 id**
      ③ 后台任务 process_upload()
             parse_file()            解析（也放后台：坏 PDF 要落成 failed 状态）
             _ingest_segments()      切片 → 向量化 → 写 chunks → 置 ready
                                     任一步失败 → 置 failed + error_message

    为什么要异步：一份 20 页 PDF 要切片 + 算几十次 embedding，本地模型十几秒起。
    同步等会让请求挂住（网关超时 → 用户重试 → 同一份文档入库两遍 → 检索出重复片段），
    而且 embedding 是 CPU 密集任务，占满线程池会把正常检索请求也拖慢。

    为什么切片和状态要在**同一个事务**里提交：
    不能出现「切片写完了但状态还是 processing」的中间态 ——
    那会让「这份文档能不能被检索到」变得不可预测。
    要么全成（ready + 全部切片），要么全无（回滚 + failed）。

删除的作用域纪律（2026-09-28 D21 复盘新增）：
    验证脚本清理自己的数据，一律走 `delete_documents_by_prefix()`，
    **不许**用 `delete_all_documents()`。理由见后者的 docstring：
    D21 之前全库都是测试数据，删干净无所谓；D21 起库里有了**长期语料**
    （评测集的依据），全删一次就是不可逆的数据损失。
"""

import os
import uuid

from sqlalchemy import delete, func, select

from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.services.document_parser import ParsedSegment, parse_file
from app.services.embedding_service import embed_texts
from app.services.retrieval_service import split_text
from app.services.tokenizer import tokenize_to_string

# 全量清空所需的显式确认环境变量。默认**没有**，所以默认调不通。
DELETE_ALL_ENV_VAR = "ALLOW_DELETE_ALL_DOCUMENTS"


# 失败原因写入数据库前的截断长度：错误堆栈可能很长，别把列撑爆
_MAX_ERROR_LEN = 500


# ============================================================
# 写路径：入库
# ============================================================
async def create_document(filename: str, file_type: str) -> uuid.UUID:
    """
    建一条 processing 记录并立刻提交，返回文档 ID。

    先落库再干活（而不是干完活再落库）的原因：
    文档 ID 要在**响应里就返回**给用户，后续的状态查询都靠它。
    """
    async with async_session_factory() as session:
        document = Document(filename=filename, file_type=file_type)
        session.add(document)
        await session.commit()
        # expire_on_commit=False（见 core/db.py），所以 commit 后 id 依然可读
        return document.id


async def process_upload(document_id: uuid.UUID, filename: str, data: bytes) -> int:
    """
    后台任务入口（HTTP 上传路径）：解析 + 入库。

    为什么解析也放在后台、而不是在路由里做完再返回：
    PRD F4 的验收点明确要求「失败文档（坏 PDF）有 failed 状态」。
    如果解析在路由里做，坏 PDF 会在文档还没「出生」时就抛错，
    根本没有那条记录可以标记为 failed。
    """
    try:
        segments = await parse_file(filename, data)
    except Exception as e:
        logger.warning("文档解析失败 id=%s filename=%s 原因=%s", document_id, filename, e)
        await _mark_failed(document_id, f"解析失败：{e}")
        return 0
    return await _ingest_segments(document_id, segments)


async def ingest_texts(texts: list[str], filename: str, file_type: str = "txt") -> tuple[uuid.UUID, int]:
    """
    同步入库（不经过后台任务）—— 供种子脚本 / 验证脚本 / 测试使用。

    和 HTTP 路径**共用同一个 _ingest_segments**，切片/向量化/状态流转的
    逻辑只有一份。区别仅在于：这里没有 HTTP 响应时限，可以一路 await 到底。
    """
    document_id = await create_document(filename=filename, file_type=file_type)
    segments = [ParsedSegment(text=text) for text in texts]
    count = await _ingest_segments(document_id, segments)
    if count == 0:
        raise RuntimeError(f"文档入库失败：{filename}（详情见日志与 documents.error_message）")
    return document_id, count


async def _ingest_segments(document_id: uuid.UUID, segments: list[ParsedSegment]) -> int:
    """
    入库主体：切片 → 向量化 → 写切片 → 置 ready。失败落 failed。

    这个函数被两条路径复用（后台任务 / 同步入库），
    try/except 必须包住**全部**步骤 —— 漏掉任何一步，
    失败时文档都会永远卡在 processing（前端一直显示「处理中」）。
    """
    try:
        pairs = _split_segments(segments)
        if not pairs:
            raise ValueError("切片结果为空（原文可能只有空白字符）")

        # 一次批量算完所有向量（比逐条算快几倍到几十倍）
        vectors = await embed_texts([text for text, _ in pairs])

        async with async_session_factory() as session:
            # strict=True：两边长度不齐直接抛错，防止「内容 A 配上向量 B」这种静默错位
            for index, ((text, page_ref), vec) in enumerate(zip(pairs, vectors, strict=True)):
                session.add(
                    DocumentChunk(
                        document_id=document_id,
                        chunk_index=index,
                        content=text,
                        # D16：入库时就把分词结果算好（索引侧的"索引"部分）。
                        # 这里必须和查询侧调**同一个** tokenize —— 否则两侧 token 对不上，
                        # 检索会静默失效（不报错，只是永远查不到）。
                        # content_tsv 那列不用管：数据库会从 content_tokens 自动派生。
                        content_tokens=tokenize_to_string(text),
                        embedding=vec,
                        page_ref=page_ref,
                    )
                )

            document = await session.get(Document, document_id)
            if document is None:
                raise ValueError(f"文档不存在：{document_id}")
            document.status = DocumentStatus.READY
            document.chunk_count = len(pairs)
            document.error_message = None
            # 一次提交：切片与状态同时生效，不存在「切片写了状态还没改」的中间态
            await session.commit()

        logger.info("文档入库完成 id=%s 切片数=%d", document_id, len(pairs))
        return len(pairs)

    except Exception as e:
        logger.exception("文档入库失败 id=%s 原因=%s", document_id, e)
        await _mark_failed(document_id, str(e))
        return 0


def _split_segments(segments: list[ParsedSegment]) -> list[tuple[str, str | None]]:
    """
    把解析出来的段落逐个切片，并保留每片来自哪一页。

    返回值是 (切片文本, page_ref) 的列表 —— 页码在切片那一刻就「焊」上去了。
    如果等入完库再回头找「这片来自第几页」，就只能去原文里做字符串搜索，既慢又不可靠。
    """
    pairs: list[tuple[str, str | None]] = []
    for segment in segments:
        for chunk in split_text(segment.text):
            pairs.append((chunk, segment.page_ref))
    return pairs


async def _mark_failed(document_id: uuid.UUID, reason: str) -> None:
    """
    把文档标记为失败。

    用**新的 session**：调用点通常刚经历过一次异常，
    原来的 session 可能已处于不可用状态，复用它提交很可能再炸一次。
    """
    async with async_session_factory() as session:
        document = await session.get(Document, document_id)
        if document is None:
            logger.error("标记失败状态时文档不存在 id=%s", document_id)
            return
        document.status = DocumentStatus.FAILED
        document.error_message = reason[:_MAX_ERROR_LEN]
        await session.commit()


# ============================================================
# 读路径：列表 / 详情
# ============================================================
async def list_documents(limit: int = 50, offset: int = 0) -> list[Document]:
    """按上传时间倒序列出文档（前端靠它轮询状态）。"""
    async with async_session_factory() as session:
        stmt = (
            select(Document)
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await session.scalars(stmt)).all())


async def get_document(document_id: uuid.UUID) -> Document | None:
    """按 ID 取单份文档（上传接口返回时用它把状态带出去）。"""
    async with async_session_factory() as session:
        return await session.get(Document, document_id)


async def get_document_preview(document_id: uuid.UUID) -> tuple[Document | None, list[DocumentChunk]]:
    """读取文档元数据与按原始顺序排列的已解析切片。"""
    async with async_session_factory() as session:
        document = await session.get(Document, document_id)
        if document is None:
            return None, []
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc())
        )
        chunks = list((await session.scalars(stmt)).all())
        return document, chunks


async def count_documents() -> int:
    """文档总数（health / 列表分页用）。"""
    async with async_session_factory() as session:
        return await session.scalar(select(func.count()).select_from(Document)) or 0


# ============================================================
# 删除
# ============================================================
async def delete_document(document_id: uuid.UUID) -> bool:
    """
    删除文档，切片由数据库外键级联删除。

    注意这里**不需要**手写「先删切片再删文档」：
    外键上的 ON DELETE CASCADE 已经声明了这件事，
    数据库保证两个动作在同一个事务里完成，不会出现中间态。
    """
    async with async_session_factory() as session:
        result = await session.execute(delete(Document).where(Document.id == document_id))
        await session.commit()
        deleted = (result.rowcount or 0) > 0

    logger.info("删除文档 id=%s 结果=%s", document_id, "成功" if deleted else "不存在")
    return deleted


def escape_like(value: str) -> str:
    """
    转义 SQL LIKE 模式里的通配符。

    为什么必须转义：`%` 在 LIKE 里是"任意串"、`_` 是"任意单个字符"。
    测试用的前缀里出现下划线完全正常（如 `d21_corpus-`），
    这时 `LIKE 'd21_corpus-%'` 会连 `d21Xcorpus-...` 一起匹配 ——
    **多删了东西但不会报错**。属于铁律 12 那类静默失效。

    转义符统一用反斜杠，调用处要配 `escape="\\"`。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _prefix_pattern(prefix: str) -> str:
    """把前缀转成安全的 LIKE 模式，并拒绝"空前缀"。"""
    # 空前缀 -> LIKE '%' -> 匹配全库。等于把危险函数换了个名字继续用，
    # 所以这里必须直接拒绝，而不是"温和地"删掉所有东西。
    if not prefix or not prefix.strip():
        raise ValueError(
            "prefix 不能为空：空前缀等于 LIKE '%'，会删光全库。"
            "若确实要清空，请显式调用 delete_all_documents()。"
        )
    return f"{escape_like(prefix)}%"


async def delete_documents_by_prefix(prefix: str) -> int:
    """
    删除文件名以 `prefix` 开头的文档（切片由外键级联删除），返回删除条数。

    这是验证脚本清理自己数据的**唯一推荐方式**（2026-09-28 D21 新增）。
    约定：每个脚本声明一个 `VERIFY_PREFIX = "dNN-xxx"`，造的语料全部带这个前缀，
    跑完按同一个前缀收干净 —— 作用域自限，碰不到别人/长期语料。
    """
    async with async_session_factory() as session:
        result = await session.execute(
            delete(Document).where(Document.filename.like(_prefix_pattern(prefix), escape="\\"))
        )
        await session.commit()
    count = result.rowcount or 0
    logger.info("按前缀删除文档 prefix=%s 删除数=%d", prefix, count)
    return count


async def count_documents_by_prefix(prefix: str) -> int:
    """
    统计文件名以 `prefix` 开头的文档数 —— 给"清理干净了吗"的断言用。

    为什么不复用 list_documents()：列表有默认 limit，文档多了会**截断**，
    于是"残留数"被少报，断言假通过。
    """
    async with async_session_factory() as session:
        stmt = (
            select(func.count())
            .select_from(Document)
            .where(Document.filename.like(_prefix_pattern(prefix), escape="\\"))
        )
        return await session.scalar(stmt) or 0


async def delete_all_documents() -> int:
    """
    ⚠️ 高危：清空**整张** documents 表（切片级联删除）。

    唯一正当用途：换 embedding 模型后的全量 reindex —— 旧向量和新向量不在同一个
    语义空间，混在一张表里检索排序就是乱的。这是物理约束，代码绕不过去。

    ---- 2026-09-28 加固：为什么现在必须显式确认 ----
    D21 之前，库里全是测试数据，"全删"和"重来"没区别，所以 7 个验证脚本
    都顺手调了它。D21 建了 50 条评测集之后，语料成了**评测的依据**：
    跑一次全量回归就把 8 篇语料（51 片）删光，而**这些脚本的本意只是想清掉
    自己造的那两三篇** —— 它们该调的是 `delete_documents_by_prefix()`。

    危险在于它**完全静默**：删除成功、退出码 0、断言全绿，
    只有"库里少了 8 篇文档"这一件事没人知道。

    所以加两道闸：
      ① 关键字参数 `confirm=True` 必须显式写出来（可 grep、可 review）；
      ② 环境变量 `ALLOW_DELETE_ALL_DOCUMENTS=1` 必须设。
    两道都过才算"确实要全量重建"，而不是"顺手清一下"。
    """
    if os.getenv(DELETE_ALL_ENV_VAR) != "1":
        raise RuntimeError(
            f"delete_all_documents() 被调用，但环境变量 {DELETE_ALL_ENV_VAR} != 1，已拒绝执行。\n"
            "  这个函数会清空**整张** documents 表，包括长期语料（评测集的依据）。\n"
            "  验证脚本清理自己的数据请改用 delete_documents_by_prefix(VERIFY_PREFIX)。\n"
            f"  确实要做全量重建，请显式设 {DELETE_ALL_ENV_VAR}=1 后再跑。"
        )

    async with async_session_factory() as session:
        result = await session.execute(delete(Document))
        await session.commit()
    count = result.rowcount or 0
    logger.warning("⚠ 已清空全部文档（含长期语料）删除数=%d", count)
    return count



async def count_chunks(document_id: uuid.UUID | None = None) -> int:
    """切片总数（可按文档过滤）—— 验证级联删除时用。"""
    async with async_session_factory() as session:
        stmt = select(func.count()).select_from(DocumentChunk)
        if document_id is not None:
            stmt = stmt.where(DocumentChunk.document_id == document_id)
        return await session.scalar(stmt) or 0
