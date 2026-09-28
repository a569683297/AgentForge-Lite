"""
D21 数据落地：入库 8 篇语料 + 写入 50 条评测集 + 打印内容指纹
================================================================
顺序不能反（这是 D21 最重要的一条工程约束）：

    ① 立语料（真实尺寸的中文文档入库）
            ↓  ← 必须先在库里，题才有"依据"
    ② 出题（scripts/d21_cases.py 里的 50 条）
            ↓
    ③ 落库 + 算指纹（写完即冻结）

反过来的典型失败：先想 50 个"像样的问题"，再去找语料 —— 结果参考答案里的数字
（"年假 5 天"）在语料里根本不存在，**这题永远答不对，而且不会报错**，
只会在 D23 报告里表现为"某个配置特别差"。

--------------------------------------------------------------------------
幂等性
--------------------------------------------------------------------------
默认：已入库的语料**跳过**（按文件名判断），评测集**清空重写**。
    · 语料跳过 —— 重建会生成新的文档 UUID，而评测集的 doc_ids 指向旧 UUID，
      两张表就对不上了（这正是 D21 的"静默数据失联"）
    · 评测集重写 —— 它是纯数据、无外键依赖，重写才能保证"库里 == 源文件"

`--force`：把 D21 的 8 篇语料删掉重建（改了语料内容之后用），
           重建后会自动重新解析 slug → 新 UUID 并重写评测集。

运行（必须在项目根目录）：
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d21_seed
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d21_seed --force
"""

import argparse
import asyncio

import app.models  # noqa: F401
from app.core.db import async_session_factory
from app.services import document_service, eval_service
from scripts.d21_corpus import DOCUMENTS, SLUG_TO_FILENAME, full_text
from scripts.d21_cases import CASES, summarize
from sqlalchemy import delete, select

from app.models.document import Document


async def existing_documents() -> dict[str, object]:
    """库里已存在的 D21 语料：文件名 → 文档对象（用于幂等判断与 slug 映射）。"""
    filenames = list(SLUG_TO_FILENAME.values())
    async with async_session_factory() as session:
        rows = (await session.scalars(
            select(Document).where(Document.filename.in_(filenames))
        )).all()
    return {row.filename: row for row in rows}


async def drop_corpus() -> int:
    """删除 D21 的 8 篇语料（切片由数据库外键级联删除）。"""
    filenames = list(SLUG_TO_FILENAME.values())
    async with async_session_factory() as session:
        result = await session.execute(delete(Document).where(Document.filename.in_(filenames)))
        await session.commit()
    return result.rowcount or 0


async def ingest_corpus(force: bool) -> dict[str, object]:
    """入库语料，返回 slug → 文档对象 的映射。"""
    if force:
        removed = await drop_corpus()
        print(f"  [--force] 已删除旧语料 {removed} 篇，开始重建")

    existed = await existing_documents()
    slug_to_doc = {}
    total_chunks = 0

    for slug, filename, lines in DOCUMENTS:
        text = full_text(lines)
        doc = existed.get(filename)
        if doc is None:
            doc_id, chunks = await document_service.ingest_texts([text], filename=filename)
            doc = await document_service.get_document(doc_id)
            print(f"  入库 {filename[:26]:28s} 原文 {len(text):4d} 字 → {chunks} 片")
        else:
            chunks = doc.chunk_count
            print(f"  跳过 {filename[:26]:28s} 已存在（{chunks} 片，{len(text)} 字）")
        slug_to_doc[slug] = doc
        total_chunks += chunks

    # 正向断言：切片数必须够。少于 20 片就装不满重排的候选窗口（RERANK_CANDIDATE_K=20），
    # "top20 → 重排 → top5" 这条链路根本测不出东西（D19 的 B1 断言就是这条）
    if total_chunks <= 20:
        raise RuntimeError(f"语料切片总数只有 {total_chunks}，不足以支撑重排候选窗口（需 > 20）")
    print(f"  语料合计 {len(DOCUMENTS)} 篇 / {total_chunks} 片")
    return slug_to_doc


async def main() -> None:
    parser = argparse.ArgumentParser(description="D21 语料与评测集落地")
    parser.add_argument("--force", action="store_true", help="删除并重建 D21 语料（改了语料内容后用）")
    args = parser.parse_args()

    print("=" * 74)
    print("D21 数据落地：语料 → 评测集 → 指纹")
    print("=" * 74)

    print("[1/3] 入库语料")
    slug_to_doc = await ingest_corpus(force=args.force)

    # slug → UUID 映射：从**库里的实际对象**里取，而不是"这次刚入库的返回值"。
    # 差别在于幂等场景：跳过入库的那些文档，这次并没有返回值，但映射照样要成立。
    slug_to_doc_id = {slug: doc.id for slug, doc in slug_to_doc.items()}
    if len(slug_to_doc_id) != len(DOCUMENTS):
        raise RuntimeError(f"slug 映射不完整：{sorted(slug_to_doc_id)}")

    print("[2/3] 写入评测集")
    count, fingerprint = await eval_service.seed_cases(CASES, slug_to_doc_id)
    print(f"  写入 {count} 条")

    print("[3/3] 交叉核对与统计")
    # 源文件 vs 库：两边算出来的指纹必须一致。
    # 不一致说明落库过程丢了信息（或 seed 逻辑有 bug）—— 这是"冻结"能成立的前提
    db_fingerprint, db_count = await eval_service.fingerprint_from_db()
    source_fingerprint = eval_service.compute_dataset_fingerprint(CASES)
    if db_fingerprint != source_fingerprint:
        raise RuntimeError(
            f"指纹不一致！源文件={source_fingerprint[:16]}… 库中={db_fingerprint[:16]}… "
            f"（落库过程改变了内容或丢了字段）"
        )
    if db_count != len(CASES):
        raise RuntimeError(f"条数不一致：源文件 {len(CASES)} 条，库中 {db_count} 条")
    print(f"  [OK] 源文件指纹 == 库中指纹：{fingerprint}")

    stats = summarize()
    print()
    print("  类别分布：", stats["by_category"])
    print("  难度分布：", stats["by_difficulty"])
    print(f"  负例：{stats['negative']} 条")
    print(f"  覆盖语料：{len(stats['covered_slugs'])}/{len(DOCUMENTS)} 篇")

    # 有语料没被任何题用到 = 白写；这不算错误，但值得报出来
    unused = set(SLUG_TO_FILENAME) - set(stats["covered_slugs"])
    if unused:
        print(f"  ℹ 未被任何题目引用的语料：{sorted(unused)}")
    else:
        print("  [OK] 8 篇语料全部被题目引用")

    print()
    print("=" * 74)
    print(f"评测集指纹（冻结依据）：{fingerprint}")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
