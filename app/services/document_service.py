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
"""

import uuid

from sqlalchemy import delete, func, select

from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.services.document_parser import ParsedSegment, parse_file
from app.services.embedding_service import embed_texts
from app.services.retrieval_service import split_text

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


async def delete_all_documents() -> int:
    """
    清空全部文档（切片级联删除）。

    用途：换 embedding 模型后必须全量 reindex —— 旧向量和新向量不在同一个
    语义空间，混在一张表里检索排序就是乱的。这是物理约束，代码绕不过去。
    """
    async with async_session_factory() as session:
        result = await session.execute(delete(Document))
        await session.commit()
    count = result.rowcount or 0
    logger.info("已清空全部文档 删除数=%d", count)
    return count


async def count_chunks(document_id: uuid.UUID | None = None) -> int:
    """切片总数（可按文档过滤）—— 验证级联删除时用。"""
    async with async_session_factory() as session:
        stmt = select(func.count()).select_from(DocumentChunk)
        if document_id is not None:
            stmt = stmt.where(DocumentChunk.document_id == document_id)
        return await session.scalar(stmt) or 0
