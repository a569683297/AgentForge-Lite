"""
文档管理接口（D12）
====================
POST   /api/documents         上传（multipart）→ 立刻返回 processing，后台解析入库
GET    /api/documents         列表（带状态，前端轮询它看进展）
DELETE /api/documents/{id}    删除（切片由数据库外键级联删除）

路由层的三件事：校验输入 → 建记录 → 派后台任务。业务逻辑全在 document_service。
"""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, Response, UploadFile, status

from app.core.logging import logger
from app.schemas.document import DocumentOut, DocumentPreviewOut
from app.services import document_service
from app.services.document_parser import check_upload

router = APIRouter(tags=["documents"])


@router.post(
    "/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传文档（异步入库）",
)
async def upload_document(
    background: BackgroundTasks,
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
    """
    filename = file.filename or "untitled"
    data = await file.read()

    try:
        file_type = check_upload(filename, data)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    document_id = await document_service.create_document(filename=filename, file_type=file_type)

    # 派后台任务：响应发回给客户端之后才执行。
    # 注意它跑在**同一个进程**里 —— 进程重启任务就没了，文档会卡在 processing。
    # 可接受的取舍：我们要的是链路完整，不是生产级任务队列（阶段 2 再上 Celery/ARQ）。
    background.add_task(document_service.process_upload, document_id, filename, data)

    logger.info("收到上传 filename=%s 大小=%dB 文档ID=%s", filename, len(data), document_id)
    document = await document_service.get_document(document_id)
    if document is None:                                    # 理论上不可能，防御性处理
        raise HTTPException(status_code=500, detail="文档创建失败")
    return DocumentOut.model_validate(document)


@router.get("/documents", response_model=list[DocumentOut], summary="文档列表")
async def list_documents(
    limit: int = Query(default=50, ge=1, le=200, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量（分页）"),
) -> list[DocumentOut]:
    """按上传时间倒序列出文档；前端靠它轮询 processing → ready/failed 的变化。"""
    documents = await document_service.list_documents(limit=limit, offset=offset)
    return [DocumentOut.model_validate(doc) for doc in documents]


@router.get("/documents/{document_id}/preview", response_model=DocumentPreviewOut, summary="文档详情与文本预览")
async def preview_document(document_id: UUID) -> DocumentPreviewOut:
    """返回文档元数据和已解析切片；不暴露原始文件或任何凭据。"""
    document, chunks = await document_service.get_document_preview(document_id)
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
async def delete_document(document_id: UUID) -> Response:
    """
    删除一份文档及其全部切片。

    路径参数声明成 UUID 而不是 str：格式不对时 FastAPI 直接返回 422，
    不用手写解析和校验，也避免非法字符串被当成主键去查库。
    """
    deleted = await document_service.delete_document(document_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
