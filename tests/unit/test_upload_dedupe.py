"""
上传幂等判定（块 1）：内容指纹去重 + failed 原地重试
=====================================================
三层断言，分别对应三个设计决定：

1. **指纹算原始字节**（纯函数）：解析失败的文件也照样有指纹。
2. **状态决定行为**（真库）：ready 拒收、processing 拒收、failed 复用同一条记录。
3. **CAS 抢锁**（真库 + 真并发）：两个请求同时重试同一份失败文档，
   恰好一个抢到、一个被拒 —— 这是唯一一条能证明"不会并行跑两遍"的用例。

`db` 标记的用例会写库，但**每个用例都在 finally 里按前缀清理**，
不碰长期语料（约定见 document_service.delete_documents_by_prefix 的 docstring）。
"""

import asyncio
import hashlib
import uuid

import pytest
from sqlalchemy import func, select, update

from app.core.db import async_session_factory
from app.models.document import Document, DocumentStatus
from app.services import document_service
from app.services.document_parser import parse_file

# 所有本文件造的数据都带这个前缀，finally 里按它收干净
TEST_PREFIX = "rag-dedupe-test-"


# ============================================================
# 一、指纹（纯逻辑，不碰库）
# ============================================================
def test_hash_is_sha256_of_raw_bytes():
    data = b"hello world"
    digest = document_service.compute_content_hash(data)

    assert digest == hashlib.sha256(data).hexdigest()
    assert len(digest) == 64
    assert digest == digest.lower()
    assert set(digest) <= set("0123456789abcdef")


def test_same_bytes_same_hash_even_with_different_filenames():
    """身份是内容，不是文件名 —— 同内容换个名字传，仍然要判为同一份。"""
    data = "报销标准".encode()
    assert document_service.compute_content_hash(data) == document_service.compute_content_hash(data)


def test_different_bytes_different_hash():
    assert document_service.compute_content_hash(b"a") != document_service.compute_content_hash(b"b")


async def test_hash_exists_even_when_parsing_fails():
    """
    ★ 核心收益：坏文件在**解析之前**就拿到了指纹。

    如果指纹来自解析文本，这个用例根本不成立 —— 解析抛异常，
    后面算指纹的代码一行都不会执行，于是 failed 记录永远是 NULL，
    用户重传同一份文件时系统认不出它，只能再建一条新的失败记录。
    """
    broken_pdf = b"\x00\x01\x02 definitely not a pdf"
    assert len(document_service.compute_content_hash(broken_pdf)) == 64

    with pytest.raises(Exception):
        await parse_file("broken.pdf", broken_pdf)


# ============================================================
# 二、状态裁决（真库）
# ============================================================
async def _cleanup() -> None:
    await document_service.delete_documents_by_prefix(TEST_PREFIX)


async def _force_status(document_id, status: str, error: str | None = None) -> None:
    """把记录强推到指定状态（模拟"后台任务失败了"）。"""
    async with async_session_factory() as session:
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(status=status, error_message=error, updated_at=func.now())
        )
        await session.commit()


async def _count_by_hash(content_hash: str) -> int:
    async with async_session_factory() as session:
        stmt = select(func.count()).select_from(Document).where(Document.content_hash == content_hash)
        return await session.scalar(stmt) or 0


def _fresh_hash() -> str:
    """每次用不同的内容，避免和历史数据/其他用例撞指纹。"""
    return document_service.compute_content_hash(uuid.uuid4().bytes)


@pytest.mark.db
async def test_first_upload_creates_and_second_is_rejected():
    content_hash = _fresh_hash()
    try:
        first, outcome = await document_service.begin_upload(
            f"{TEST_PREFIX}a.txt", "txt", content_hash
        )
        assert outcome == document_service.UploadOutcome.CREATED
        assert first.status == DocumentStatus.PROCESSING

        # 第二次（哪怕换了文件名）应命中同一条记录，且不新建
        second, outcome2 = await document_service.begin_upload(
            f"{TEST_PREFIX}b.txt", "txt", content_hash
        )
        assert outcome2 == document_service.UploadOutcome.DUPLICATE_PROCESSING
        assert second.id == first.id
        assert await _count_by_hash(content_hash) == 1
    finally:
        await _cleanup()


@pytest.mark.db
async def test_ready_document_is_rejected_as_duplicate():
    content_hash = _fresh_hash()
    try:
        document, _ = await document_service.begin_upload(f"{TEST_PREFIX}ready.txt", "txt", content_hash)
        await _force_status(document.id, DocumentStatus.READY)

        duplicate, outcome = await document_service.begin_upload(
            f"{TEST_PREFIX}ready-again.txt", "txt", content_hash
        )
        assert outcome == document_service.UploadOutcome.DUPLICATE_READY
        assert duplicate.id == document.id
        assert await _count_by_hash(content_hash) == 1
    finally:
        await _cleanup()


@pytest.mark.db
async def test_failed_document_is_reused_in_place():
    """
    ★ "失败后重新上传" 的完整语义：复用原记录、清掉失败痕迹、改回 processing。

    重点看三个字段：error_message 必须清空（否则前端一直显示上次的错误）、
    chunk_count 归零、created_at **不变**（它是历史事实，重试不该改写）。
    """
    content_hash = _fresh_hash()
    try:
        document, _ = await document_service.begin_upload(f"{TEST_PREFIX}fail.txt", "txt", content_hash)
        created_at = document.created_at
        await _force_status(document.id, DocumentStatus.FAILED, "解析失败：坏 PDF")

        retried, outcome = await document_service.begin_upload(
            f"{TEST_PREFIX}fail-retry.txt", "txt", content_hash
        )
        assert outcome == document_service.UploadOutcome.RETRIED
        assert retried.id == document.id                     # 同一条记录，不是新建
        assert retried.status == DocumentStatus.PROCESSING
        assert retried.error_message is None
        assert retried.chunk_count == 0
        assert retried.filename == f"{TEST_PREFIX}fail-retry.txt"   # 展示用最新名字
        assert retried.created_at == created_at
        assert await _count_by_hash(content_hash) == 1
    finally:
        await _cleanup()


@pytest.mark.db
async def test_concurrent_retry_claims_the_lock_exactly_once():
    """
    ★ CAS 的证明题：两个请求同时重试同一条 failed 记录，只能有一个抢到。

    没有 `WHERE status='failed'` 时，这条用例会稳定失败 ——
    两个请求都会拿到 RETRIED，然后并排跑两遍切片。
    这正是它在防止的那种**不报错**的事故。
    """
    content_hash = _fresh_hash()
    try:
        document, _ = await document_service.begin_upload(f"{TEST_PREFIX}race.txt", "txt", content_hash)
        await _force_status(document.id, DocumentStatus.FAILED, "上一次失败")

        results = await asyncio.gather(
            document_service.begin_upload(f"{TEST_PREFIX}race-1.txt", "txt", content_hash),
            document_service.begin_upload(f"{TEST_PREFIX}race-2.txt", "txt", content_hash),
        )
        outcomes = sorted(outcome for _, outcome in results)
        assert outcomes == [
            document_service.UploadOutcome.DUPLICATE_PROCESSING,
            document_service.UploadOutcome.RETRIED,
        ], f"CAS 失效：两个请求都抢到了 → {outcomes}"

        assert {doc.id for doc, _ in results} == {document.id}
        assert await _count_by_hash(content_hash) == 1
    finally:
        await _cleanup()


@pytest.mark.db
async def test_seed_path_without_hash_keeps_null_and_does_not_conflict():
    """
    种子/验证脚本走 `create_document()`（手里只有文本、没有原始字节）→ hash 为 NULL。

    多个 NULL 必须能共存：唯一索引不约束 NULL。
    这条用例同时钉住"不拿文本临时凑一个指纹"的决定 —— 两套口径混在一列里比不去重更危险。
    """
    ids = []
    try:
        for name in ("seed-a.txt", "seed-b.txt"):
            ids.append(await document_service.create_document(f"{TEST_PREFIX}{name}", "txt"))

        async with async_session_factory() as session:
            for document_id in ids:
                document = await session.get(Document, document_id)
                assert document.content_hash is None
    finally:
        await _cleanup()
