"""
评测服务（D21）—— 评测集的写入、读取、冻结指纹
================================================
与 retrieval_service / document_service 的分工不变：本模块只管评测集的**数据**，
不跑评测、不打分（那是 D22/D23 的事）。

端到端流程（本日实际用到的两条）：

    写入（scripts/d21_seed.py 调用）
        slug_to_doc_id   ← 语料入库后拿到的 slug → 文档 UUID 映射
        score→ seed_cases(cases, slug_to_doc_id)
            ① 清空 eval_cases（幂等：重复跑不会翻倍）
            ② 把 doc_slugs 翻译成 doc_ids 写入
            ③ 返回内容指纹

    读取（GET /api/eval/cases）
        list_cases(category?) → ORM 列表 → schemas 序列化

--------------------------------------------------------------------------
内容指纹：把"写完即冻结"从口头约定变成可机检的事实
--------------------------------------------------------------------------
不冻结的具体危害是可预测的：跑完看一眼哪条挂了 → 顺手改 prompt 或改题 → 重跑 →
分数涨了 → 那是**把评测集背下来了**（数据泄漏 / 过拟合评测集），不是效果提升。
口头约定无法证明"题目没被改过"，指纹可以。

指纹的输入**只包含影响判分的内容**，每个字段的取舍都有理由：

    进指纹：case_key / category / difficulty / is_negative / question / reference
            / evidence / doc_slugs / expected_tool
    不进指纹：id            ← bigserial，重新写入就会变
              created_at    ← 与内容无关
              doc_ids       ← ⚠ 这是关键的一条：doc_ids 是**文档 UUID**，
                              而每次重建语料都会生成新的 UUID。若把它算进指纹，
                              "题目一个字没改、只是重跑了一次 seed" 也会被判成改版
                              → 报警疲劳 → 冻结机制被绕过。所以指纹用 doc_slugs。

三条稳定性要求（少一条就会出"假报警"）：
    ① 按 case_key 排序 —— 库返回的行序不保证稳定，顺序一变指纹就变
    ② sort_keys=True   —— JSON 对象键序不参与语义，不该影响指纹
    ③ 补默认值          —— 缺键与显式 None 必须归一，否则同一内容两种指纹
"""

import hashlib
import json
import uuid

from sqlalchemy import delete, func, select

from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.eval import EvalCase


# ============================================================
# 指纹
# ============================================================
def _normalized_payload(raw) -> dict:
    """
    把一条用例归一成"参与指纹计算"的字典。

    同时接受两种输入，且**必须产出完全一样的结果**：
        - dict      —— 出题脚本（scripts/d21_cases.py）里的原始数据
        - EvalCase  —— 从数据库读回来的 ORM 对象

    为什么强调"必须一样"：写入前算一次指纹、以后每次跑评测从库里读回来再算一次，
    两者不等就说明**落库过程丢了信息或被悄悄改过**。这本就是一条强断言
    （d21_eval_verify.py 的 E 段就在验它），前提是两条路径产出同一个字典。
    """
    get = (lambda k, d=None: raw.get(k, d)) if isinstance(raw, dict) else \
          (lambda k, d=None: getattr(raw, k, d))

    return {
        "key": get("case_key", ""),
        "category": get("category", ""),
        "difficulty": get("difficulty", ""),
        "negative": bool(get("is_negative", False)),
        "question": get("question", ""),
        "reference": get("reference", ""),
        "evidence": get("evidence", ""),
        # 排序：该字段本身是"文档集合"，顺序不该影响指纹（A+B 与 B+A 是同一套依据）
        "slugs": sorted(get("doc_slugs") or []),
        # 归一 None ↔ 哨兵值以外的空：expected_tool 为 None 时统一成 ""，
        # 否则 dict 路径给 None、ORM 路径给 None 也对得上，但一旦有人写成 "" 就会假报警
        "tool": get("expected_tool") or "",
    }


def compute_dataset_fingerprint(cases) -> str:
    """
    计算评测集内容指纹（sha256 十六进制）。

    参数可以是 dict 列表、ORM 对象列表，或两者的混合 —— 归一函数负责抹平差异。
    """
    payload = [_normalized_payload(c) for c in cases]
    payload.sort(key=lambda item: item["key"])            # ① 固定顺序
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))  # ② 紧凑且键有序
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def fingerprint_from_db() -> tuple[str, int]:
    """从库里读回全部用例算指纹，返回 (指纹, 条数)。D22 起每次跑评测先调它校验。"""
    async with async_session_factory() as session:
        rows = list((await session.scalars(select(EvalCase))).all())
    return compute_dataset_fingerprint(rows), len(rows)


# ============================================================
# 写入
# ============================================================
async def seed_cases(cases: list[dict], slug_to_doc_id: dict[str, uuid.UUID]) -> tuple[int, str]:
    """
    全量写入评测集（先清空再写，可重复执行）。

    为什么"先清空"而不是"逐条 upsert"：
      重跑 seed 时，如果上次有、这次没有的题（比如删掉了 A31），upsert 会把它留下 ——
      于是**库里的评测集与 d21_cases.py 不再一致**，而指纹只反映库里的内容，
      报告会拿到一个"包含幽灵题"的评测集。清空重写让库始终等于源文件。

    Returns:
        (写入条数, 内容指纹)
    """
    rows: list[EvalCase] = []
    missing_slugs: set[str] = set()

    for case in cases:
        slugs = list(case.get("doc_slugs") or [])
        doc_ids = [slug_to_doc_id[s] for s in slugs if s in slug_to_doc_id]
        # 记下翻译失败的 slug：doc_ids 少一个不会报错，只会让"这道题的依据文档"变空 ——
        # 静默缺数据是这一族里最危险的（同 D16「列加了但数据是空的」）
        missing_slugs.update(s for s in slugs if s not in slug_to_doc_id)

        rows.append(
            EvalCase(
                case_key=case["case_key"],
                category=case["category"],
                question=case["question"],
                reference=case["reference"],
                evidence=case["evidence"],
                doc_ids=doc_ids or None,
                doc_slugs=slugs,
                expected_tool=case.get("expected_tool"),
                is_negative=bool(case.get("is_negative", False)),
                difficulty=case["difficulty"],
            )
        )

    if missing_slugs:
        raise RuntimeError(f"以下 slug 在语料里找不到对应文档：{sorted(missing_slugs)}")

    async with async_session_factory() as session:
        await session.execute(delete(EvalCase))
        session.add_all(rows)
        await session.commit()

    fingerprint = compute_dataset_fingerprint(rows)
    logger.info("评测集写入完成 条数=%d 指纹=%s", len(rows), fingerprint[:16])
    return len(rows), fingerprint


# ============================================================
# 读取
# ============================================================
async def list_cases(
    category: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[EvalCase]:
    """按 case_key 排序列出用例（顺序稳定，便于比对与分页）。"""
    async with async_session_factory() as session:
        stmt = select(EvalCase).order_by(EvalCase.case_key).limit(limit).offset(offset)
        if category:
            stmt = stmt.where(EvalCase.category == category)
        return list((await session.scalars(stmt)).all())


async def count_cases(category: str | None = None) -> int:
    """用例总数（可按类别过滤）。"""
    async with async_session_factory() as session:
        stmt = select(func.count()).select_from(EvalCase)
        if category:
            stmt = stmt.where(EvalCase.category == category)
        return await session.scalar(stmt) or 0


async def get_case_by_key(case_key: str) -> EvalCase | None:
    """按业务键取单条（验证脚本与 D22 复现单条结果时用）。"""
    async with async_session_factory() as session:
        return await session.scalar(select(EvalCase).where(EvalCase.case_key == case_key))


async def dataset_stats() -> dict:
    """评测集的分布统计（供 /api/eval/dataset 与验证脚本使用）。"""
    async with async_session_factory() as session:
        rows = list((await session.scalars(select(EvalCase))).all())

    by_category: dict[str, int] = {}
    by_difficulty: dict[str, int] = {}
    slugs: set[str] = set()
    for row in rows:
        by_category[row.category] = by_category.get(row.category, 0) + 1
        by_difficulty[row.difficulty] = by_difficulty.get(row.difficulty, 0) + 1
        slugs.update(row.doc_slugs or [])

    return {
        "total": len(rows),
        "negative": sum(1 for r in rows if r.is_negative),
        "by_category": by_category,
        "by_difficulty": by_difficulty,
        "covered_slugs": sorted(slugs),
        "fingerprint": compute_dataset_fingerprint(rows),
    }
