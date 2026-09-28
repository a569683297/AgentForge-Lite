"""
D12 验证脚本：文档管理接口（上传 / 列表 / 删除）
=================================================
覆盖：
  V1-V5  四种格式上传（md / txt-GBK / docx / pdf）与失败路径（无文字层 PDF）
  V6-V7  不支持的扩展名 / 超大文件 → 400
  V8     起点状态：新建记录就是 processing（不派后台任务，直接看库）
  V9     列表能反映各文档状态
  V10    端到端检索：上传的虚构事实能被 search 命中
  V11    只检索 ready 文档：processing 文档的切片检索不到
  V12    删除 → 切片被数据库级联删除
  V13    删除不存在 / 非法 ID → 404 / 422

两个先说清楚的行为（否则会误判）：

① 为什么用 httpx.ASGITransport 而不是 fastapi 的 TestClient：
   TestClient 会在**独立线程里起一个自己的事件循环**，而本脚本还要直接 await
   数据库操作（查切片数、手动改状态）。两个事件循环抢同一个连接池，
   必然报 `attached to a different loop`。
   ASGITransport 让 HTTP 调用和数据库操作跑在**同一个 loop** 里，问题消失。

② 上传接口返回的 status 永远是 processing —— 这不是 bug：
   响应体是在 `background.add_task(...)` **派发之前**构造好的，
   那一刻后台任务还没开始跑。最终状态要靠后续 GET /api/documents 才能看到（V9）。
   这正是 PRD 要的语义：接口返回「起点状态」，不是「结果状态」。
   （BackgroundTasks 确实会在本次 ASGI 调用结束前跑完，所以紧随其后的 V9
     已经能读到 ready / failed —— 但那是「另一次请求看到的结果」。）

用法：uv run python -m scripts.d12_documents_verify
"""

import asyncio
import io
import uuid

from docx import Document as DocxDocument
from httpx import ASGITransport, AsyncClient
from pypdf import PdfWriter

import app.models  # noqa: F401
from app.core.db import async_session_factory
from app.main import app
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.services.document_service import (
    count_chunks,
    count_documents_by_prefix,
    create_document,
    delete_documents_by_prefix,
)
from app.services.embedding_service import embed_texts
from app.services.retrieval_service import search

# 虚构事实：模型不可能知道，命中即证明真的走了检索
FAKE_FACT = "「玄鸟」项目由数据治理组负责，负责人为秦工，项目周期十八个月。"

# 本脚本造的全部文档都带此前缀（2026-09-28 新增）—— 清理时的作用域边界。
# 原先调 delete_all_documents() 清空全库，会删光 D21 的长期语料。
# 之所以不能像别的脚本那样只给一个文件名加前缀：本脚本要造 9 份不同文件
# （四种格式 + 失败路径 + 待删除），**没有共同前缀就没法把清理范围框住**。
VERIFY_PREFIX = "d12"


def section(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def build_text_pdf() -> bytes:
    """
    手工拼一个「两页、每页一句话」的最小 PDF。

    为什么不装生成库：只为验证页边界与 page_ref 的透传，
    手写一个最简 PDF 就够了，不必为测试引入 reportlab 这种重量级依赖。
    """
    texts = [
        "The XuanNiao project is owned by the data governance team.",
        "The QingNiao project is owned by the platform infrastructure team.",
    ]
    page_ids = [4 + i for i in range(len(texts))]
    content_ids = [4 + len(texts) + i for i in range(len(texts))]

    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {len(texts)} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, text in enumerate(texts):
        objs[page_ids[i]] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_ids[i]} 0 R >>"
        ).encode()
        stream = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET".encode()
        objs[content_ids[i]] = (
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for oid in sorted(objs):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n".encode() + objs[oid] + b"\nendobj\n"

    max_id = max(objs)
    xref_pos = len(out)
    out += f"xref\n0 {max_id + 1}\n".encode() + b"0000000000 65535 f \n"
    for oid in range(1, max_id + 1):
        out += f"{offsets[oid]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


def build_blank_pdf() -> bytes:
    """一个没有文字层的 PDF（相当于扫描件/纯图片）——用于制造解析失败。"""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def build_docx(text: str) -> bytes:
    """用 python-docx 现场生成一个 docx 供上传。"""
    document = DocxDocument()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def upload(client: AsyncClient, filename: str, data: bytes):
    return await client.post(
        "/api/documents",
        files={"file": (filename, data, "application/octet-stream")},
    )


# ============================================================
# 用例
# ============================================================
async def case_upload_formats(client: AsyncClient) -> tuple[bool, dict]:
    """
    V1-V5：四种格式 + 失败路径的**接口契约**。

    这里只断言「已受理」：202 + status=processing + chunk_count=0。
    最终是否真的变成 ready / failed，由 V9 查列表来判定 ——
    两件事分开验，才不会把「接口语义」和「状态流转」搅在一起。
    """
    section("V1-V5. 上传各种格式（接口契约：已受理）")
    ids: dict[str, str] = {}
    ok = True

    cases = [
        ("d12-formats.md", "文档格式说明：公司所有内部文档统一存放在知识库中。".encode("utf-8"), "md"),
        ("d12-gbk.txt", "员工考勤规定：上班时间为上午九点，迟到超过三十分钟记一次迟到。".encode("gbk"), "txt(GBK)"),
        ("d12-policy.docx", build_docx(f"产品规划：{FAKE_FACT}"), "docx"),
        ("d12-report.pdf", build_text_pdf(), "pdf（两页，应带页码）"),
        ("d12-scanned.pdf", build_blank_pdf(), "PDF 无文字层（应失败）"),
    ]
    for filename, data, label in cases:
        response = await upload(client, filename, data)
        body = response.json() if response.status_code < 300 else {}
        accepted = (
            response.status_code == 202
            and body.get("status") == DocumentStatus.PROCESSING
            and body.get("chunk_count") == 0
        )
        ok = ok and accepted
        ids[label] = body.get("id", "")
        print(
            f"  [{'✅' if accepted else '❌'}] {label:<22} HTTP={response.status_code} "
            f"status={body.get('status')} 切片={body.get('chunk_count')}"
        )

    return ok, ids


async def case_pdf_page_ref(client: AsyncClient, ids: dict) -> bool:
    """V5b：PDF 的切片是否真的带上了页码。"""
    section("V5b. PDF 页码（page_ref）")
    pdf_id = uuid.UUID(ids["pdf（两页，应带页码）"])
    async with async_session_factory() as session:
        rows = await session.execute(
            DocumentChunk.__table__.select()
            .where(DocumentChunk.document_id == pdf_id)
            .order_by(DocumentChunk.chunk_index)
        )
        refs = [row.page_ref for row in rows]

    print(f"  切片数={len(refs)}，页码={refs}")
    hit = len(refs) == 2 and refs == ["p.1", "p.2"]
    print(f"  [{'✅' if hit else '❌'}] 两页各一片且页码正确")

    results = await search("Who owns the XuanNiao project", top_k=2)
    print("  检索结果里的 page_ref：")
    for item in results:
        print(f"    {item['source']}  {item['page_ref']}  相似度={item['similarity']}")
    carried = any(item["page_ref"] for item in results)
    print(f"  [{'✅' if carried else '❌'}] 页码透传到了检索结果（引用能精确到页）")
    return hit and carried


async def case_bad_input(client: AsyncClient) -> bool:
    """V6-V7：扩展名与大小校验（都应 400，且不产生文档记录）。"""
    section("V6-V7. 非法上传")
    ok = True

    bad_ext = await upload(client, "d12-virus.exe", b"MZ\x90\x00")
    hit = bad_ext.status_code == 400
    ok = ok and hit
    print(f"  [{'✅' if hit else '❌'}] 不支持的扩展名 → HTTP={bad_ext.status_code} detail={bad_ext.json().get('detail')}")

    too_big = await upload(client, "d12-big.txt", b"0" * (21 * 1024 * 1024))
    hit = too_big.status_code == 400
    ok = ok and hit
    print(f"  [{'✅' if hit else '❌'}] 超大文件(21MB)   → HTTP={too_big.status_code} detail={too_big.json().get('detail')}")

    return ok


async def case_initial_status() -> tuple[bool, uuid.UUID]:
    """V8：起点状态是 processing（绕开 HTTP，因为没有后台任务派发）。"""
    section("V8. 起点状态（直接建记录，不派后台任务）")
    document_id = await create_document(filename="d12-pending.md", file_type="md")
    async with async_session_factory() as session:
        document = await session.get(Document, document_id)
        status_value = document.status if document else None
        chunk_count = document.chunk_count if document else -1
    hit = status_value == DocumentStatus.PROCESSING and chunk_count == 0
    print(f"  [{'✅' if hit else '❌'}] 新建文档 status={status_value} chunk_count={chunk_count}（期望 processing / 0）")
    return hit, document_id


async def case_list(client: AsyncClient) -> bool:
    """V9：列表反映各文档状态。"""
    section("V9. 文档列表")
    response = await client.get("/api/documents")
    documents = response.json()
    print(f"  HTTP={response.status_code} 共 {len(documents)} 份：")
    for doc in documents:
        print(
            f"    {doc['filename']:<16} {doc['status']:<10} 切片={doc['chunk_count']:<3} "
            f"错误={doc['error_message'] or '-'}"
        )

    by_name = {doc["filename"]: doc for doc in documents}
    checks = {
        "四种格式都在列表里": all(
            name in by_name for name in ("d12-formats.md", "d12-gbk.txt", "d12-policy.docx", "d12-report.pdf")
        ),
        "失败文档状态为 failed": by_name.get("d12-scanned.pdf", {}).get("status") == DocumentStatus.FAILED,
        "成功文档状态为 ready": by_name.get("d12-formats.md", {}).get("status") == DocumentStatus.READY,
        "processing 文档也在列表里": by_name.get("d12-pending.md", {}).get("status") == DocumentStatus.PROCESSING,
    }
    for label, hit in checks.items():
        print(f"  [{'✅' if hit else '❌'}] {label}")
    return all(checks.values())


async def case_search_after_upload() -> bool:
    """V10：上传的内容能被检索到（端到端闭环）。"""
    section("V10. 上传后能被检索到")
    results = await search("玄鸟项目是谁负责的", top_k=3)
    print(f"  检索「玄鸟项目是谁负责的」→ {len(results)} 条")
    for item in results:
        print(f"    来源={item['source']}  页码={item['page_ref']}  相似度={item['similarity']}")
    hit = any("玄鸟" in item["content"] and item["source"] == "d12-policy.docx" for item in results)
    print(f"  [{'✅' if hit else '❌'}] 命中 docx 里上传的虚构事实")
    return hit


async def case_only_ready_is_searchable(pending_id: uuid.UUID) -> bool:
    """
    V11：只检索 ready 文档。

    做法：给 V8 那个还在 processing 的文档手工塞一个切片（带真实向量）
         → 它的内容不应该被检索到；把它改成 ready 后 → 应该被检索到。
    这是「半截数据会不会污染检索」的直接验证。
    """
    section("V11. 只检索 ready 文档的切片")
    marker = "青龙山会议室的预约规则是提前三天，由行政部门统一登记。"
    vector = (await embed_texts([marker]))[0]

    async with async_session_factory() as session:
        session.add(
            DocumentChunk(
                document_id=pending_id,
                chunk_index=0,
                content=marker,
                embedding=vector,
                page_ref=None,
            )
        )
        await session.commit()

    while_processing = await search("青龙山会议室怎么预约", top_k=3)
    blocked = not any(marker in item["content"] for item in while_processing)
    print(f"  [{'✅' if blocked else '❌'}] processing 文档的切片检索不到（命中 {len(while_processing)} 条）")

    async with async_session_factory() as session:
        document = await session.get(Document, pending_id)
        document.status = DocumentStatus.READY
        document.chunk_count = 1
        await session.commit()

    after_ready = await search("青龙山会议室怎么预约", top_k=3)
    visible = any(marker in item["content"] for item in after_ready)
    print(f"  [{'✅' if visible else '❌'}] 改成 ready 后立刻可检索（命中 {len(after_ready)} 条）")

    return blocked and visible


async def case_delete(client: AsyncClient) -> bool:
    """V12-V13：删除 + 级联 + 错误码。"""
    section("V12. 删除与级联")
    ok = True

    created = await upload(client, "d12-to-delete.md", "这是一份待删除的文档，用于验证级联删除。".encode())
    document_id = created.json()["id"]
    chunks_before = await count_chunks(uuid.UUID(document_id))
    print(f"  删除前：文档 {document_id} 有 {chunks_before} 个切片")

    response = await client.delete(f"/api/documents/{document_id}")
    chunks_after = await count_chunks(uuid.UUID(document_id))
    cascaded = chunks_before > 0 and chunks_after == 0
    ok = ok and response.status_code == 204 and cascaded
    print(f"  [{'✅' if response.status_code == 204 else '❌'}] DELETE → HTTP={response.status_code}")
    print(f"  [{'✅' if cascaded else '❌'}] 切片被级联删除：{chunks_before} → {chunks_after}")

    remaining = {doc["id"] for doc in (await client.get("/api/documents")).json()}
    gone = document_id not in remaining
    ok = ok and gone
    print(f"  [{'✅' if gone else '❌'}] 列表里已不含该文档")

    section("V13. 错误码")
    missing = await client.delete(f"/api/documents/{uuid.uuid4()}")
    hit = missing.status_code == 404
    ok = ok and hit
    print(f"  [{'✅' if hit else '❌'}] 删除不存在的文档 → HTTP={missing.status_code}")

    malformed = await client.delete("/api/documents/not-a-uuid")
    hit = malformed.status_code == 422
    ok = ok and hit
    print(f"  [{'✅' if hit else '❌'}] 非法 UUID          → HTTP={malformed.status_code}")

    return ok


async def cleanup_corpus() -> None:
    """跑完把自己的语料收干净 —— 别给 D22/D23 的检索评测留下会命中的垃圾。

    ⚠ 本脚本的 V1-V5 上传了 5 份、V8 手工建了 1 份，V12 只删了其中 1 份。
    剩下的如果不收，就会一直躺在知识库里被检索到。
    """
    await delete_documents_by_prefix(VERIFY_PREFIX)
    left = await count_documents_by_prefix(VERIFY_PREFIX)
    print(f"\n（已清理本脚本语料：{VERIFY_PREFIX}* 残留={left}）")
    if left:
        raise RuntimeError(f"清理不干净：{VERIFY_PREFIX}* 还剩 {left} 份")


async def main() -> None:
    print(f"{'=' * 64}\n清理本脚本自己的数据（保证本次验证干净）\n{'=' * 64}")
    deleted = await delete_documents_by_prefix(VERIFY_PREFIX)
    print(f"  已清理 {deleted} 份本脚本的文档（切片由外键级联删除）")
    print(f"  ℹ 前缀之外的既有文档（含 D21 长期语料）不受影响")
    results: list[tuple[str, bool]] = []

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        ok, ids = await case_upload_formats(client)
        results.append(("V1-V5 四种格式 + 失败路径", ok))

        results.append(("V5b PDF 页码透传", await case_pdf_page_ref(client, ids)))
        results.append(("V6-V7 非法上传校验", await case_bad_input(client)))

        ok, pending_id = await case_initial_status()
        results.append(("V8 起点状态 processing", ok))

        results.append(("V9 文档列表", await case_list(client)))

        results.append(("V10 上传后可检索", await case_search_after_upload()))
        results.append(("V11 只检索 ready 切片", await case_only_ready_is_searchable(pending_id)))
        results.append(("V12-V13 删除与级联", await case_delete(client)))

    # 跑完把自己的语料收干净 —— 别给 D22/D23 的检索评测留下会命中的垃圾
    await cleanup_corpus()

    section("汇总")
    for name, passed in results:
        print(f"  [{'✅' if passed else '❌'}] {name}")
    failed = [name for name, passed in results if not passed]
    print(f"\n{'全部通过' if not failed else '未通过：' + '，'.join(failed)}")


if __name__ == "__main__":
    asyncio.run(main())
