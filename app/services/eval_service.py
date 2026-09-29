"""
评测服务（D21 起）—— 评测集、语料指纹、评测运行与明细的数据层
================================================================
与 retrieval_service / document_service 的分工不变：本模块只管评测相关的**数据**，
不跑被测系统、不打分（那是 `eval_runner` + `judge_service` 的事）。

D22 新增三块能力：
    语料指纹    compute_corpus_fingerprint / corpus_fingerprint_from_db
    运行与明细   create_run / finish_run / insert_case_results
    聚合下钻    list_runs / list_case_results / aggregate_by_category / failure_breakdown

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
两个指纹，缺一不可（D22 补上第二个）
--------------------------------------------------------------------------
    dataset_fingerprint  评测集指纹 —— "考卷封了吗"
    corpus_fingerprint   语料指纹   —— "教材也封了吗"

D21 只有前者，等于**考卷封了、教材可以随时换**。已实测的后果：改
`scripts/d21_corpus.py` 的一句正文、不带 `--force` 重跑 seed → 语料根本没更新、
评测集逐字相同、指纹不变，**三处都不出声**。

D23 的核心比较是"同一份题、同一份语料、三个配置"，所以两者都必须冻。

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

⚠ 同一条纪律反过来读：这两个指纹都**不含 UUID**，所以"重建语料不报警"
  与"语料换了也不报警"是同一行代码的两面 —— 这正是 D22 要同时用
  **切片内容**（含切片方式）做语料指纹、并把校验放在 seed 里显式拦的原因。
"""

import hashlib
import json
import uuid

from sqlalchemy import Integer, cast, delete, func, select

from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.eval import EvalCase, EvalCaseResult, EvalRun


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


# ============================================================
# 语料指纹（D22 新增）—— "教材封了吗"
# ============================================================
def compute_corpus_fingerprint(chunks_by_file: dict[str, list[str]]) -> str:
    """
    语料内容指纹（sha256 十六进制）。

    覆盖面 = **正文内容 + 切片方式**：
        · 正文改一个字          → 变
        · 改了 CHUNK_SIZE/OVERLAP 并重建语料 → 变
          （这是**要的**：切片方式变了检索结果就会变，三个配置的对比就不公平了）
        · 重建语料（新的 UUID） → **不变**（与 dataset 指纹同一条纪律：UUID 不进指纹）

    为什么键用**文件名**而不是 slug：
        指纹要在两条路径上算出同一个值 ——
            源文件侧：scripts/d21_seed.py 手里是 slug
            库里侧  ：document_chunks 只知道自己属于哪个 filename
        用 filename 做键，两条路径都能拿到；用 slug 则库里侧取不到。

    为什么用 dict 而不是拼接成一个长字符串：
        拼接需要分隔符，而分隔符可能与正文冲突（正文里真的出现 "|||" 就会撞）。
        结构化序列化 + `sort_keys` 天然无歧义。
    """
    payload = {name: list(chunks) for name, chunks in sorted(chunks_by_file.items())}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def corpus_fingerprint_from_db(filenames: list[str] | None = None) -> tuple[str, int]:
    """
    从库里读回切片算语料指纹，返回 (指纹, 切片总数)。

    Args:
        filenames: 只算这些文件的切片；None = **全库**。

    ⚠ 语料**原文不存在库里** —— `documents` 表只有 filename/status/chunk_count，
      正文只以切片形式存在于 `document_chunks.content`（见 app/models/document.py）。
      所以指纹只能由切片算出。这不是将就：切片本身就是检索看到的东西，
      而且顺带把"切片方式"纳入了冻结范围（见 compute_corpus_fingerprint 的说明）。

    ⚠ 评测运行要传 **None（全库）** 而不是那 8 个文件名。理由：
      检索是在**整个知识库**上做的，别的脚本留下的测试残留同样会被命中。
      全库指纹因此把"D23 跑三个配置之前库必须是同一个状态"变成机器可查的事实 ——
      跑一次回归留下几片残留，指纹就会变，而不是让三个配置悄悄在三个不同的库上比。
      （seed 的源文件核对相反：它只关心自己那 8 篇，所以传 filenames。）
    """
    async with async_session_factory() as session:
        stmt = (
            select(Document.filename, DocumentChunk.chunk_index, DocumentChunk.content)
            .join(DocumentChunk, DocumentChunk.document_id == Document.id)
            .order_by(Document.filename, DocumentChunk.chunk_index)
        )
        if filenames is not None:
            stmt = stmt.where(Document.filename.in_(filenames))
        rows = (await session.execute(stmt)).all()

    grouped: dict[str, list[tuple[int, str]]] = {}
    for filename, chunk_index, content in rows:
        grouped.setdefault(filename, []).append((chunk_index, content))

    # 显式再排一次：SQL 的 ORDER BY 已经排好，但"指纹依赖 chunk_index 顺序"
    # 这个前提不该依赖调用方的 order_by 写法（将来加个 join 就可能被打乱，
    # 而打乱后指纹会变，表现为"语料被改过"的假报警）。
    chunks_by_file = {
        name: [content for _, content in sorted(items)]
        for name, items in grouped.items()
    }
    return compute_corpus_fingerprint(chunks_by_file), len(rows)


# ============================================================
# 评测运行（eval_runs）—— 汇总
# ============================================================
async def create_run(
    *,
    config_name: str,
    dataset_fingerprint: str,
    corpus_fingerprint: str,
    judge_model: str,
    generation_runs: int,
    runs_per_case: int,
) -> int:
    """
    新建一条评测运行，返回 run_id。三个维度均分与准确率**先留空**，跑完由 finish_run 回填。

    为什么先建 run 再跑（而不是跑完一次性写入）：
        明细表的 `run_id` 是外键，明细要在跑的过程中逐批落库。
        若跑完才建 run，就得把 50×N 条明细全存在内存里等最后一次写入 ——
        中途崩掉则全部丢失，而"跑一次 20 分钟"的实验最怕这个。

    为什么指纹必须在这里就写进去（而不是记在别处）：
        报告要能回答"这个分数是哪版评测集、哪版语料、哪个 judge、重复几次跑出来的"。
        这四个答案必须是**数据里带的**，不能靠事后回忆或翻日志
        （同 D19：降级必须可归因，否则降级样本会被算成正常成绩）。
    """
    async with async_session_factory() as session:
        run = EvalRun(
            config_name=config_name,
            dataset_fingerprint=dataset_fingerprint,
            corpus_fingerprint=corpus_fingerprint,
            judge_model=judge_model,
            generation_runs=generation_runs,
            runs_per_case=runs_per_case,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)     # 取回数据库生成的自增 id
    logger.info(
        "评测运行已创建 run_id=%d config=%s gen_runs=%d judge_runs=%d",
        run.id, config_name, generation_runs, runs_per_case,
    )
    return run.id


async def finish_run(run_id: int, summary: dict, report_path: str | None = None) -> None:
    """回填汇总分（由 summarize_results 算出，本函数只负责写）。"""
    async with async_session_factory() as session:
        run = await session.get(EvalRun, run_id)
        if run is None:
            raise RuntimeError(f"eval_runs 中不存在 run_id={run_id} —— 不能给不存在的运行回填分数")
        run.score_correctness = summary["score_correctness"]
        run.score_faithfulness = summary["score_faithfulness"]
        run.score_completeness = summary["score_completeness"]
        run.accuracy = summary["accuracy"]
        run.report_path = report_path
        await session.commit()


# ============================================================
# 评测明细（eval_case_results）
# ============================================================
# 必填字段清单。**为什么要有这份清单**：`EvalCaseResult(**row)` 对**缺失**的键
# 会静默使用默认值（score=None / passed=False），于是"少写了一列"会变成
# "这条题答错了" —— 数据看起来完全正常，只是分数是假的。
# 多出的键会因未知 kwarg 抛 TypeError（这一侧是安全的），缺的那一侧必须自己拦。
_REQUIRED_RESULT_FIELDS = frozenset({
    "case_key", "category", "generation_index", "answer", "retrieved_chunks",
    "tool_calls", "sources_count", "score_correctness", "score_faithfulness",
    "score_completeness", "judge_runs", "judge_raw", "passed", "failure_reason",
})


async def insert_case_results(run_id: int, rows: list[dict]) -> int:
    """批量写入评测明细。返回写入条数。"""
    if not rows:
        return 0

    for index, row in enumerate(rows):
        missing = _REQUIRED_RESULT_FIELDS - set(row)
        if missing:
            # 报错时带上 case_key：只说"第 3 行缺字段"的话，还要人去数一遍才知道是哪条题
            raise ValueError(
                f"明细第 {index} 行（case_key={row.get('case_key')!r}）缺少必填字段 "
                f"{sorted(missing)} —— 缺字段会用默认值落库，"
                "把'少写了一列'变成'这条题答错了'"
            )

    async with async_session_factory() as session:
        session.add_all([EvalCaseResult(run_id=run_id, **row) for row in rows])
        await session.commit()
    return len(rows)


def _field(row, key: str, default=None):
    """
    统一取值：同时接受 dict（runner 里刚算出来的行）与 ORM 对象（从库里读回来的行）。

    为什么必须两者都支持：同一个 `summarize_results` 会被用在两处 ——
        · runner 跑完当场算汇总（手里是 dict）
        · API 读历史运行时重算汇总（手里是 EvalCaseResult 对象）
    两处**必须给出同一个结果**，否则"报告里写的准确率"与"接口读出来的准确率"
    会不一致，而这是最难发现的一类错（两份数字都"有来源"）。
    验证脚本据此有一条断言：当场算的 == 从库重算的。
    """
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def summarize_results(rows: list) -> dict:
    """
    从明细行算出汇总（**"准确率"定义的唯一出处**，纯函数、可单测）。

    输入可以是 dict 列表或 ORM 对象列表（用 `_field` 抹平差异）。

    ------------------------------------------------------------------
    两个口径必须分开说清（这是 D20 留下的教训：不同口径的数字不能并列比较）
    ------------------------------------------------------------------
    accuracy（准确率）—— 按**题**算：
        每道题先在它自己的 N 次生成里做多数投票 → 得到"这道题过没过"，
        再算通过题数 / 总题数。
        为什么按题：PRD 的 "+10%" 说的是**题目**的百分比（50 条题里多对 4 条）。
        按行算的话，generation_runs 一变分母就变，"+10%" 立刻不可解释。

    三维度均分 —— 按**行**算（每次生成的答案各算一份）：
        它衡量的是"系统产出的东西平均质量如何"，天然是逐次生成的粒度。
        ⚠ 所以它与 accuracy 的**分母不同**，报告里必须分别写明，不能互相印证。

    多数投票的平局规则：N 为偶数时可能 1:1，此时记为**不通过**
        （`sum*2 > len` 严格大于）。理由同 D20：平局说明"没有多数意见"，
        把平局算成通过等于**往有利方向兜底**，会让系统看起来比实际好。
    """
    if not rows:
        return {
            "total_rows": 0, "total_cases": 0, "passed_cases": 0, "accuracy": 0.0,
            "score_correctness": None, "score_faithfulness": None, "score_completeness": None,
            "failure_breakdown": {}, "generation_inconsistent": 0,
        }

    by_case: dict[str, list[bool]] = {}
    for row in rows:
        by_case.setdefault(str(_field(row, "case_key")), []).append(bool(_field(row, "passed")))

    passed_cases = sum(1 for votes in by_case.values() if sum(votes) * 2 > len(votes))
    # 生成侧不一致的题数：同一条题的多次生成里"过"与"不过"都出现过。
    # 这是 D21 发现的"生成侧抖动"的**直接观测**，报告要单独报出来 ——
    # 它一高，说明"这个分数"里有一部分只是随机性，而不是配置差异。
    inconsistent = sum(1 for votes in by_case.values() if 0 < sum(votes) < len(votes))

    def mean_of(field: str) -> float | None:
        values = [v for v in (_field(row, field) for row in rows) if v is not None]
        return round(sum(values) / len(values), 4) if values else None

    breakdown: dict[str, int] = {}
    for row in rows:
        if _field(row, "passed"):
            continue
        reason = _field(row, "failure_reason") or "unknown"
        breakdown[reason] = breakdown.get(reason, 0) + 1

    return {
        "total_rows": len(rows),
        "total_cases": len(by_case),
        "passed_cases": passed_cases,
        "accuracy": round(passed_cases / len(by_case), 4),
        "score_correctness": mean_of("score_correctness"),
        "score_faithfulness": mean_of("score_faithfulness"),
        "score_completeness": mean_of("score_completeness"),
        "failure_breakdown": breakdown,
        "generation_inconsistent": inconsistent,
    }


async def list_case_results(
    run_id: int,
    category: str | None = None,
    passed: bool | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[EvalCaseResult]:
    """列出某次运行的明细（可按类别 / 通过与否过滤），按 (case_key, generation_index) 稳定排序。"""
    async with async_session_factory() as session:
        stmt = (
            select(EvalCaseResult)
            .where(EvalCaseResult.run_id == run_id)
            .order_by(EvalCaseResult.case_key, EvalCaseResult.generation_index)
            .limit(limit)
            .offset(offset)
        )
        if category is not None:
            stmt = stmt.where(EvalCaseResult.category == category)
        if passed is not None:
            stmt = stmt.where(EvalCaseResult.passed == passed)
        return list((await session.scalars(stmt)).all())


async def list_runs(limit: int = 50, offset: int = 0) -> list[EvalRun]:
    """列出评测运行（按创建时间倒序 —— 最新那批在最上面）。"""
    async with async_session_factory() as session:
        stmt = (
            select(EvalRun)
            .order_by(EvalRun.created_at.desc(), EvalRun.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await session.scalars(stmt)).all())


async def get_run(run_id: int) -> EvalRun | None:
    async with async_session_factory() as session:
        return await session.get(EvalRun, run_id)


async def aggregate_by_category(run_id: int) -> list[dict]:
    """
    按类别聚合（PRD F7.10 的固定查询，也是 Agentic BI 下钻链的第 2 跳）。

    ⚠ 这里是**行级**聚合（分母是明细行数），与 `summarize_results` 的按题准确率
      不是同一个口径。它回答的是"哪一类的平均分更差"，不是"哪一类通过率更低"。
    """
    async with async_session_factory() as session:
        stmt = (
            select(
                EvalCaseResult.category,
                func.count().label("rows"),
                func.count(func.distinct(EvalCaseResult.case_key)).label("cases"),
                # passed 是 boolean，PostgreSQL 的 sum() 不接受 boolean，
                # 必须显式 cast 成 integer —— 不 cast 会报 "function sum(boolean) does not exist"，
                # 而这属于"跑起来才知道"的错，写的时候不显眼。
                func.sum(cast(EvalCaseResult.passed, Integer)).label("passed_rows"),
                func.avg(EvalCaseResult.score_correctness).label("avg_correctness"),
                func.avg(EvalCaseResult.score_faithfulness).label("avg_faithfulness"),
                func.avg(EvalCaseResult.score_completeness).label("avg_completeness"),
            )
            .where(EvalCaseResult.run_id == run_id)
            .group_by(EvalCaseResult.category)
            .order_by(EvalCaseResult.category)
        )
        result = await session.execute(stmt)
        return [
            {
                "category": row.category,
                "rows": row.rows,
                "cases": row.cases,
                "passed_rows": int(row.passed_rows or 0),
                "avg_correctness": round(float(row.avg_correctness), 3) if row.avg_correctness is not None else None,
                "avg_faithfulness": round(float(row.avg_faithfulness), 3) if row.avg_faithfulness is not None else None,
                "avg_completeness": round(float(row.avg_completeness), 3) if row.avg_completeness is not None else None,
            }
            for row in result
        ]


async def count_case_results(run_id: int) -> int:
    """某次运行的明细条数（验收点"明细表有行"直接用它）。"""
    async with async_session_factory() as session:
        return await session.scalar(
            select(func.count()).select_from(EvalCaseResult).where(EvalCaseResult.run_id == run_id)
        ) or 0


async def delete_run(run_id: int) -> tuple[int, int]:
    """
    删除一次评测运行（明细由外键 CASCADE 带走），返回 (删除的 run 数, 被带走的明细数)。

    ⚠ **按 run_id 删，不是清空表** —— 这条纪律是 D21 用一次真实事故换来的：
      当时 9 个验证脚本调用 `delete_all_documents()`，跑一次全量回归就把
      51 片长期语料删光，而且不报错。清理函数的作用域**必须自限**。
      所以这里宁可多写一个参数，也不提供"清空全部评测运行"的接口。
    """
    async with async_session_factory() as session:
        detail_count = await session.scalar(
            select(func.count()).select_from(EvalCaseResult).where(EvalCaseResult.run_id == run_id)
        ) or 0
        result = await session.execute(delete(EvalRun).where(EvalRun.id == run_id))
        await session.commit()
    return result.rowcount or 0, detail_count
