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

--------------------------------------------------------------------------
D22 补上的一个漏洞：改了语料但忘了 --force
--------------------------------------------------------------------------
改动之前的实际行为（实测过，不是推演）：

    改了 d21_corpus.py 里某篇的一句正文 → 不带 --force 跑 seed →
    语料按文件名判断"已存在" → **整篇跳过，一行写库动作都没有** →
    评测集照旧重写（源文件没改，逐字相同）→ 指纹不变 → **三处都不出声**。
    你以为改了语料，库里还是旧的。

危险在于它**看起来完全成功**：脚本打印一串"跳过 xxx 已存在"、指纹一致、
`d21_eval_verify` 全绿（只有当被改的那句正好被某条 evidence 引用时才会红）。

所以现在加了一道**前置校验**：把源文件按同样的切分规则算出"预期语料指纹"，
与库里的实际指纹比对。不一致就报错、并提示用 `--force`。

⚠ 这同时暴露了一个反面性质：**"重建语料后指纹不变"与"语料换了也不报警"
  是同一行代码的两面**（都因为指纹不含 UUID）。前者是 D21 的好消息，
  后者是 D21 的盲区 —— 同一性质的两种读法。

运行（必须在项目根目录）：
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d21_seed
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d21_seed --force
"""

import argparse
import asyncio

import app.models  # noqa: F401
from app.core.db import async_session_factory
from app.services import document_service, eval_service
from app.services.retrieval_service import split_text
from scripts.d21_corpus import DOCUMENTS, SLUG_TO_FILENAME, full_text
from scripts.d21_cases import CASES, summarize
from sqlalchemy import delete, select

from app.models.document import Document


def expected_corpus_fingerprint() -> str:
    """
    源文件侧的"预期语料指纹"。

    必须与库里侧用**同一套切分**：入库走的是
    `ingest_texts → _split_segments → split_text`（单点出口），
    这里直接调同一个 `split_text`，所以两边算出来的切片序列必然一致。
    如果哪天入库逻辑绕开了 split_text，这个函数会立刻报不一致 ——
    这正是它作为"独立来源"的价值（而不是照着库里的结果反推）。
    """
    chunks_by_file = {
        SLUG_TO_FILENAME[slug]: split_text(full_text(lines))
        for slug, _filename, lines in DOCUMENTS
    }
    return eval_service.compute_corpus_fingerprint(chunks_by_file)


async def check_corpus_fingerprint(force: bool) -> str | None:
    """
    语料指纹前置校验。返回库里当前的指纹（库里为空则返回 None）。

    `--force` 时直接跳过检查：那次运行本来就要删了重建，
    拦下来只会要求人跑两遍（先 --force 再正常跑），没有意义。
    """
    if force:
        return None

    filenames = list(SLUG_TO_FILENAME.values())
    actual, chunk_count = await eval_service.corpus_fingerprint_from_db(filenames=filenames)
    if chunk_count == 0:
        return None      # 库里还没有这批语料 → 首次运行，没什么可比

    expected = expected_corpus_fingerprint()
    if actual != expected:
        raise RuntimeError(
            "语料内容与库中不一致，但本次没有 --force —— 默认行为是"
            "\"已存在的语料整篇跳过\"，也就是说**这一轮什么都不会改**，"
            "而脚本会照常打印成功。\n"
            f"    源文件预期指纹：{expected[:32]}…\n"
            f"    库中实际指纹  ：{actual[:32]}…\n"
            "    处理办法：确认改动无误后加 --force 重建语料（会同时重写评测集）。"
        )
    return actual


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

    print("[1/4] 语料指纹前置校验")
    db_corpus_fp = await check_corpus_fingerprint(args.force)
    if db_corpus_fp:
        print(f"  [OK] 源文件预期 == 库中实际：{db_corpus_fp[:32]}…")
    else:
        print("  ℹ 库中尚无这批语料（首次运行或 --force），无可比对")

    print("[2/4] 入库语料")
    slug_to_doc = await ingest_corpus(force=args.force)

    # slug → UUID 映射：从**库里的实际对象**里取，而不是"这次刚入库的返回值"。
    # 差别在于幂等场景：跳过入库的那些文档，这次并没有返回值，但映射照样要成立。
    slug_to_doc_id = {slug: doc.id for slug, doc in slug_to_doc.items()}
    if len(slug_to_doc_id) != len(DOCUMENTS):
        raise RuntimeError(f"slug 映射不完整：{sorted(slug_to_doc_id)}")

    print("[3/4] 写入评测集")
    count, fingerprint = await eval_service.seed_cases(CASES, slug_to_doc_id)
    print(f"  写入 {count} 条")

    print("[4/4] 交叉核对与统计")
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

    # 语料指纹同理：跑完入库之后，源文件侧与库里侧必须一致。
    # 与上面"前置校验"的区别：前置校验拦的是"该重建却没重建"，
    # 这里验的是"重建之后确实落对了"（落到库里的是源文件那批内容）。
    corpus_actual, corpus_chunks = await eval_service.corpus_fingerprint_from_db(
        filenames=list(SLUG_TO_FILENAME.values())
    )
    corpus_expected = expected_corpus_fingerprint()
    if corpus_actual != corpus_expected:
        raise RuntimeError(
            f"语料指纹不一致！源文件={corpus_expected[:16]}… 库中={corpus_actual[:16]}…"
        )
    print(f"  [OK] 语料指纹（含切片方式）：{corpus_actual}  切片 {corpus_chunks} 片")

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
    print(f"语料指纹（冻结依据）  ：{corpus_actual}")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
