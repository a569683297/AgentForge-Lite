"""
文档管理接口（D12）
====================
POST   /api/documents         上传（multipart）→ 立刻返回 processing，后台解析入库
GET    /api/documents         列表（带状态，前端轮询它看进展）
DELETE /api/documents/{id}    删除（切片由数据库外键级联删除）

上传是**幂等**的（按内容指纹 sha256(原始字节) 判）：
    同一份文件第二次上传不会产生第二条记录 —— 已就绪/处理中返回 409，
    上次失败则复用原记录原地重试并返回 202。

路由层的三件事：校验输入 → 裁决（新建/重试/拒收）→ 派后台任务。
业务逻辑全在 document_service。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Response, UploadFile, status

from app.core.logging import logger
from app.core.permissions import PERM_DOC_READ, PERM_DOC_WRITE
from app.deps import require_permission
from app.models.user import User
from app.schemas.document import DocumentOut, DocumentPreviewOut
from app.models.audit_log import ACTION_DOCUMENT_DELETED
from app.services import audit_service, document_service
from app.services.document_parser import check_upload

router = APIRouter(tags=["documents"])

# 可正常受理的两种判定：全新的文档 / 复用失败记录重试
_ACCEPTED_OUTCOMES = (document_service.UploadOutcome.CREATED, document_service.UploadOutcome.RETRIED)


def _duplicate_detail(outcome: str, document) -> str:
    """
    把"拒收"的判定翻译成用户能看懂的一句话。

    为什么要在文案里带上已有文档的名字：用户手上可能同时有
    `报销标准-终版.pdf` 和 `报销标准-终版(1).pdf`，只说"已存在"等于让他自己猜是哪一份。
    """
    if outcome == document_service.UploadOutcome.DUPLICATE_READY:
        return f"《{document.filename}》已在知识库中，无需重复上传"
    return f"《{document.filename}》正在处理中，请稍候再看列表"



@router.post(
    "/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传文档（异步入库，按内容指纹幂等）",
    responses={
        409: {"description": "同一份内容已存在（已就绪或正在处理）。响应头 X-Document-Id 指向已有记录"},
    },
)
async def upload_document(
    background: BackgroundTasks,
    user: Annotated[User, Depends(require_permission(PERM_DOC_WRITE))],
    file: UploadFile = File(..., description="要入库的文件：pdf / docx / md / txt"),
) -> DocumentOut:
    """
    上传文件并入库。

    为什么返回 202 Accepted 而不是 200：
      200 的含义是「请求已经做完了」，而这里只是**被接受**了 ——
      切片、向量化还在后台跑。用 202 表达「已受理，处理未完成」才是准确语义。
      前端拿到 id 后轮询 GET /api/documents 看状态变化。

    两条失败路径分开处理：
      - 扩展名 / 大小不合格 → 这里直接 400（用户输入问题，不必建记录）
      - 解析时才发现的问题（坏 PDF）→ 后台任务里落成 failed 状态（PRD F4 验收点）

    第三条路径是**重复上传**（409）：
    同一份内容（sha256 相同）已存在时不再新建记录 —— 已就绪的拒收，上次失败的复用重试。
    这不是"额外的功能"，而是修复一个静默错误：重复入库会让同一段文本占掉 top_k 的多个名额，
    检索质量下降但**不报任何错**。
    """
    filename = file.filename or "untitled"
    data = await file.read()

    try:
        file_type = check_upload(filename, data)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    content_hash = document_service.compute_content_hash(data)
    document, outcome = await document_service.begin_upload(
        filename=filename,
        file_type=file_type,
        content_hash=content_hash,
        # 归属必须是**当前登录用户**：这份文档以后只有他能检索到、能删
        owner_id=user.id,
    )

    if outcome not in _ACCEPTED_OUTCOMES:
        logger.info(
            "拒绝重复上传 filename=%s 命中=%s 已有文档=%s", filename, outcome, document.id
        )
        # 409 + 已有文档的 id：前端可以据此定位到列表里那一行（也可当作排障线索）
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_duplicate_detail(outcome, document),
            headers={"X-Document-Id": str(document.id)},
        )

    # 派后台任务：响应发回给客户端之后才执行。
    # 注意它跑在**同一个进程**里 —— 进程重启任务就没了，文档会卡在 processing。
    # 可接受的取舍：我们要的是链路完整，不是生产级任务队列（阶段 2 再上 Celery/ARQ）。
    background.add_task(document_service.process_upload, document.id, filename, data)

    logger.info(
        "收到上传 filename=%s 大小=%dB 文档ID=%s 判定=%s",
        filename, len(data), document.id, outcome,
    )
    document = await document_service.get_document(
        document.id, owner_id=user.id
    )  # 取回 server_default 字段
    if document is None:                                    # 理论上不可能，防御性处理
        raise HTTPException(status_code=500, detail="文档创建失败")
    return DocumentOut.model_validate(document)


@router.get("/documents", response_model=list[DocumentOut], summary="文档列表")
async def list_documents(
    user: Annotated[User, Depends(require_permission(PERM_DOC_READ))],
    limit: int = Query(default=50, ge=1, le=200, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量（分页）"),
) -> list[DocumentOut]:
    """按上传时间倒序列出文档；前端靠它轮询 processing → ready/failed 的变化。

    只返回**自己的**文档（`owner_id=user.id`）—— 这是一个知识库产品最基本的边界。
    """
    documents = await document_service.list_documents(
        limit=limit, offset=offset, owner_id=user.id
    )
    return [DocumentOut.model_validate(doc) for doc in documents]


@router.get("/documents/{document_id}/preview", response_model=DocumentPreviewOut, summary="文档详情与文本预览")
async def preview_document(
    document_id: UUID,
    user: Annotated[User, Depends(require_permission(PERM_DOC_READ))],
) -> DocumentPreviewOut:
    """返回文档元数据和已解析切片；不暴露原始文件或任何凭据。

    别人的文档一律按 404 处理（不区分"不存在"与"不是你的"）。
    """
    document, chunks = await document_service.get_document_preview(
        document_id, owner_id=user.id
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
    return DocumentPreviewOut(
        document=DocumentOut.model_validate(document),
        chunks=[
            {
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
                "page_ref": chunk.page_ref,
            }
            for chunk in chunks
        ],
    )


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删除文档（级联删除切片）",
)
async def delete_document(
    document_id: UUID,
    user: Annotated[User, Depends(require_permission(PERM_DOC_WRITE))],
) -> Response:
    """
    删除一份文档及其全部切片。

    路径参数声明成 UUID 而不是 str：格式不对时 FastAPI 直接返回 422，
    不用手写解析和校验，也避免非法字符串被当成主键去查库。
    """
    deleted = await document_service.delete_document(document_id, owner_id=user.id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
    # 删除是不可逆动作（切片级联没了，原文也没留存），必须留痕
    await audit_service.record(
        action=ACTION_DOCUMENT_DELETED,
        actor_id=user.id,
        actor_email=user.email,
        target_type="document",
        target_id=document_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
