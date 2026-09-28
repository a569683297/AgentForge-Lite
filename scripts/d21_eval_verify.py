"""
D21 验证脚本：评测集与语料
============================
按 D21 的验收标准逐条验证。跑法：

    cd <项目根> && ~/.local/bin/uv run python -m scripts.d21_eval_verify

七段：
  A 表结构      —— 表/列/唯一约束真的在库里（不信"语句没报错"）
  B 语料        —— 8 篇齐全、切片数够、条款没被切在边界上
  C 评测集      —— 50 条、30/10/10、5 负例、键唯一、标注合法
  D 依据机检    —— ★ 每一条题的 evidence 必须真的对得上（今天最硬的一组断言）
  E 指纹        —— ★ 冻结机制真的成立：内容改了会变、无关的东西变了不会变
  F HTTP 接口   —— /api/eval/cases 的三个端点（PRD §13.2 的 D21 验收点）
  G 覆盖与分布  —— 语料是否都被用到、难度是否分层

⚠️ 断言全部带**正向条件**（非空、非零、条数 == 期望值）。
   否则"表里 0 行"这种空集合会让断言恒满足、假通过（铁律 9 的教训）。

⚠️ 本脚本**不修改任何数据**（只读 + 纯函数测试）。
   它验证的是 `scripts/d21_seed.py` 已落地的数据，可以反复跑。
"""

import asyncio
import datetime
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.core.db import async_session_factory, engine
from app.main import app
from app.models.document import Document
from app.models.eval import DIFFICULTIES, EVAL_CATEGORIES, EvalCase
from app.services.eval_service import compute_dataset_fingerprint
from scripts.d21_cases import CASES, summarize
from scripts.d21_corpus import DOCUMENTS, SLUG_TO_FILENAME, full_text

# 期望的分布（PRD F7.1 定死：A 30 含 5 负例 / B 10 / C 10）
EXPECTED_TOTAL = 50
EXPECTED_BY_CATEGORY = {"doc_qa": 30, "cross_doc": 10, "tool_call": 10}
EXPECTED_NEGATIVE = 5


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: bool, label: str, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  [PASS] {label}")
        else:
            self.failed.append(label)
            print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))

    def report(self) -> int:
        total = self.passed + len(self.failed)
        print()
        print("=" * 74)
        if self.failed:
            print(f"结果：{self.passed}/{total} 通过，{len(self.failed)} 失败")
            for label in self.failed:
                print(f"  ✗ {label}")
        else:
            print(f"结果：{total}/{total} 全部通过")
        print("=" * 74)
        return 1 if self.failed else 0


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def corpus_text_by_slug() -> dict[str, str]:
    """源文件里的语料全文（出题依据的权威版本）。"""
    return {slug: full_text(lines) for slug, _, lines in DOCUMENTS}


# ============================================================
# A 表结构
# ============================================================
async def section_a(ck: Checker) -> None:
    section("A 表结构（回读数据库，不信语句没报错）")

    async with async_session_factory() as session:
        rows = (await session.execute(text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' ORDER BY table_name
        """))).all()
    tables = {r.table_name for r in rows}
    print(f"  库中表清单 = {sorted(tables)}")
    ck.check("eval_cases" in tables, "A1 eval_cases 表存在")
    ck.check("eval_runs" in tables, "A2 eval_runs 表存在")

    async with async_session_factory() as session:
        cols = (await session.execute(text("""
            SELECT column_name, data_type, udt_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'eval_cases'
            ORDER BY ordinal_position
        """))).all()
    types = {c.column_name: (c.data_type, c.udt_name) for c in cols}
    print(f"  eval_cases 列 = {list(types)}")
    ck.check(len(cols) == 12, "A3 eval_cases 有 12 列", f"实际={len(cols)}")
    # 数组类型必须验 udt_name —— data_type 只说"ARRAY"，分不出 UUID[] 和 text[]
    ck.check(types.get("doc_ids") == ("ARRAY", "_uuid"),
             "A4 doc_ids 是 UUID[]（按 udt_name 判定，data_type 只说 ARRAY）",
             f"实际={types.get('doc_ids')}")
    ck.check(types.get("doc_slugs") == ("ARRAY", "_text"),
             "A5 doc_slugs 是 text[]", f"实际={types.get('doc_slugs')}")
    ck.check(types.get("is_negative", ("", ""))[0] == "boolean",
             "A6 is_negative 是 boolean", f"实际={types.get('is_negative')}")

    async with async_session_factory() as session:
        idx = (await session.execute(text("""
            SELECT indexdef FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'eval_cases'
        """))).all()
    unique_on_key = [r.indexdef for r in idx if "UNIQUE" in r.indexdef.upper()
                     and "case_key" in r.indexdef]
    ck.check(bool(unique_on_key), "A7 case_key 有唯一约束（靠应用层自觉不够）",
             f"索引={[r.indexdef for r in idx]}")


# ============================================================
# B 语料
# ============================================================
async def section_b(ck: Checker) -> None:
    section("B 语料（真实尺寸、切片充足、条款未被切在边界上）")

    filenames = list(SLUG_TO_FILENAME.values())
    async with async_session_factory() as session:
        docs = (await session.scalars(
            select(Document).where(Document.filename.in_(filenames))
        )).all()

    ck.check(len(docs) == len(DOCUMENTS), f"B1 语料 {len(DOCUMENTS)} 篇全部在库",
             f"实际={len(docs)}")
    ready = [d for d in docs if d.status == "ready"]
    ck.check(len(ready) == len(DOCUMENTS), "B2 全部 ready（没有卡在 processing 的）",
             f"ready={len(ready)}")

    total_chunks = sum(d.chunk_count for d in docs)
    ck.check(total_chunks > 20,
             "B3 切片总数 > 20（否则装不满重排的 20 条候选窗口）",
             f"实际={total_chunks}")
    print(f"  切片统计：{len(docs)} 篇 / {total_chunks} 片")

    lengths = [len(full_text(lines)) for _, _, lines in DOCUMENTS]
    ck.check(all(n >= 1000 for n in lengths),
             "B4 每篇原文 ≥ 1000 字（生产量级，不是为了跑绿而造的短句）",
             f"实际长度={lengths}")
    print(f"  原文长度：最短 {min(lengths)} / 最长 {max(lengths)} 字")

    # ★ 关键：evidence 所在的那一句必须在**某一个切片里完整存在**。
    #   如果某条款正好被切在 300 字边界上，它就会被切成两半 ——
    #   检索到也没用（半句话喂给 LLM，答案照样答不出），而这**不会报错**。
    async with async_session_factory() as session:
        chunk_rows = (await session.execute(text("""
            SELECT d.filename, c.content
            FROM document_chunks c JOIN documents d ON d.id = c.document_id
            WHERE d.filename = ANY(:names)
        """), {"names": filenames})).all()

    slug_of_filename = {v: k for k, v in SLUG_TO_FILENAME.items()}
    chunks_by_slug: dict[str, str] = {}
    for row in chunk_rows:
        slug = slug_of_filename.get(row.filename)
        if slug:
            chunks_by_slug[slug] = chunks_by_slug.get(slug, "") + "\n@@@\n" + row.content

    broken: list[str] = []
    checked = 0
    for case in CASES:
        if case["evidence"].startswith("[不存在]") or case["evidence"].startswith("[工具]"):
            continue
        for index, part in enumerate(case["evidence"].split(" | ")):
            slug = case["doc_slugs"][index]
            checked += 1
            if part not in chunks_by_slug.get(slug, ""):
                broken.append(f"{case['case_key']}：{part[:30]}…")
    ck.check(checked > 0, "B5 正向证据条目数 > 0（否则下面的断言是空集合恒真）",
             f"实际={checked} 条")
    ck.check(not broken,
             f"B5 全部 {checked} 段证据都能在**某个切片里完整出现**（没被切断）",
             f"被切断的有：{broken}")
    print(f"  逐段检查了 {checked} 段证据的切片完整性")


# ============================================================
# C 评测集
# ============================================================
async def section_c(ck: Checker) -> None:
    section("C 评测集（条数与分布按 PRD F7.1，键唯一，标注合法）")

    async with async_session_factory() as session:
        rows = list((await session.scalars(
            select(EvalCase).order_by(EvalCase.case_key)
        )).all())

    ck.check(len(rows) == EXPECTED_TOTAL, f"C1 共 {EXPECTED_TOTAL} 条", f"实际={len(rows)}")

    by_category: dict[str, int] = {}
    for row in rows:
        by_category[row.category] = by_category.get(row.category, 0) + 1
    ck.check(by_category == EXPECTED_BY_CATEGORY,
             f"C2 类别分布 = {EXPECTED_BY_CATEGORY}", f"实际={by_category}")
    print(f"  类别分布 = {by_category}")

    negatives = sum(1 for row in rows if row.is_negative)
    ck.check(negatives == EXPECTED_NEGATIVE, f"C3 负例 {EXPECTED_NEGATIVE} 条",
             f"实际={negatives}")

    keys = [row.case_key for row in rows]
    ck.check(len(set(keys)) == len(keys), "C4 case_key 无重复")
    ck.check(keys == sorted(keys), "C5 按 case_key 排序返回（顺序稳定）")

    bad_category = [r.case_key for r in rows if r.category not in EVAL_CATEGORIES]
    ck.check(not bad_category, "C6 category 全在合法取值内", f"非法={bad_category}")
    bad_difficulty = [r.case_key for r in rows if r.difficulty not in DIFFICULTIES]
    ck.check(not bad_difficulty, "C7 difficulty 全在合法取值内", f"非法={bad_difficulty}")

    # 参考答案与判定依据必须非空 —— 空 reference 会让 judge 无标尺可用（D21 原理 §3.2）
    empty_ref = [r.case_key for r in rows if not (r.reference or "").strip()]
    ck.check(not empty_ref, "C8 参考答案全部非空（judge 的标尺不能是空的）", f"空={empty_ref}")
    empty_ev = [r.case_key for r in rows if not (r.evidence or "").strip()]
    ck.check(not empty_ev, "C9 判定依据全部非空", f"空={empty_ev}")

    # 负例的参考答案必须写成"可判定陈述"，不能是 N/A 或空白
    weak = [r.case_key for r in rows
            if r.is_negative and not r.reference.startswith("知识库中")]
    ck.check(not weak,
             "C10 负例的参考答案写成「知识库中没有…」这类可判定陈述（不是 N/A）",
             f"不合规={weak}")

    # doc_ids 必须真的翻译成功（slug → UUID 失败会静默留下空数组）
    no_ids = [r.case_key for r in rows if r.doc_slugs and not r.doc_ids]
    ck.check(not no_ids, "C11 有 doc_slugs 的题，doc_ids 都已翻译成 UUID",
             f"缺 doc_ids={no_ids}")


# ============================================================
# D 依据机检（本日最重要的一组）
# ============================================================
async def section_d(ck: Checker) -> None:
    section("D 依据机检（每条题的 evidence 必须真的对得上，不靠人自觉）")

    texts = corpus_text_by_slug()
    all_corpus = "\n".join(texts.values())

    ck.check(len(texts) == len(DOCUMENTS), "D0 语料全文已加载（否则下面的断言全是空的）")

    # ---- D1 正向证据：第 i 段必须出现在第 i 个 doc_slug 的文档里 ----
    bad_positive: list[str] = []
    positive_count = 0
    for case in CASES:
        ev = case["evidence"]
        if ev.startswith("[不存在]") or ev.startswith("[工具]"):
            continue
        positive_count += 1
        parts = ev.split(" | ")
        if len(parts) != len(case["doc_slugs"]):
            bad_positive.append(f"{case['case_key']}：证据 {len(parts)} 段 vs 文档 {len(case['doc_slugs'])} 篇")
            continue
        for index, part in enumerate(parts):
            if part not in texts[case["doc_slugs"][index]]:
                bad_positive.append(f"{case['case_key']}[{index}]：{part[:28]}… 不在 {case['doc_slugs'][index]}")

    ck.check(positive_count > 0, "D1 正向证据题数 > 0", f"实际={positive_count}")
    ck.check(not bad_positive,
             f"D1 {positive_count} 条题的原文依据逐字命中（参考答案不是凭空捏造的）",
             f"不命中：{bad_positive}")

    # ---- D2 跨文档题：必须真的跨了两篇 ----
    b_cases = [c for c in CASES if c["category"] == "cross_doc"]
    ck.check(len(b_cases) == 10, "D2 跨文档题 10 条", f"实际={len(b_cases)}")
    same_doc = [c["case_key"] for c in b_cases if len(set(c["doc_slugs"])) < 2]
    ck.check(not same_doc,
             "D2 跨文档题的 doc_slugs 指向**两篇不同**文档（否则它退化成单文档题）",
             f"同篇={same_doc}")
    two_seg = [c["case_key"] for c in b_cases if len(c["evidence"].split(" | ")) < 2]
    ck.check(not two_seg, "D2 跨文档题的证据都有两段（每篇各一段）", f"只有一段={two_seg}")

    # ---- D3 负例：关键词必须在整个语料里搜不到 ----
    neg_cases = [c for c in CASES if c["is_negative"]]
    ck.check(len(neg_cases) == EXPECTED_NEGATIVE, "D3 负例 5 条", f"实际={len(neg_cases)}")
    leaked: list[str] = []
    for case in neg_cases:
        for keyword in case["evidence"].removeprefix("[不存在] ").split("|"):
            if keyword in all_corpus:
                leaked.append(f"{case['case_key']}：关键词「{keyword}」在语料里出现了")
    ck.check(not leaked,
             "D3 负例的关键词在整个语料里**确实搜不到**（否则它根本不是负例）",
             f"泄漏={leaked}")

    # ---- D4 工具题：evidence 标记与 expected_tool 一致 ----
    tool_cases = [c for c in CASES if c["category"] == "tool_call"]
    ck.check(len(tool_cases) == 10, "D4 工具题 10 条", f"实际={len(tool_cases)}")
    mismatch = [c["case_key"] for c in tool_cases
                if c["evidence"] != f"[工具] {c['expected_tool']}"]
    ck.check(not mismatch, "D4 工具题的 evidence 标记与 expected_tool 一致",
             f"不一致={mismatch}")
    # 库里也要对得上（源文件对了但落库错了，是另一回事）
    async with async_session_factory() as session:
        tool_rows = list((await session.scalars(
            select(EvalCase).where(EvalCase.category == "tool_call")
        )).all())
    db_mismatch = [r.case_key for r in tool_rows
                   if r.evidence != f"[工具] {r.expected_tool}"]
    ck.check(not db_mismatch, "D4 落库后该一致性仍然成立", f"库中不一致={db_mismatch}")

    # ---- D5 三种 evidence 形态的条数分布（能看出题目结构是否合理）----
    shapes = {"正向": 0, "负例": 0, "工具": 0}
    for case in CASES:
        if case["evidence"].startswith("[不存在]"):
            shapes["负例"] += 1
        elif case["evidence"].startswith("[工具]"):
            shapes["工具"] += 1
        else:
            shapes["正向"] += 1
    print(f"  evidence 形态分布 = {shapes}")
    ck.check(sum(shapes.values()) == EXPECTED_TOTAL,
             "D5 每条题都落进了某种 evidence 形态（没有游离在机检之外的题）",
             f"合计={sum(shapes.values())}")


# ============================================================
# E 指纹（冻结机制是否真的成立）
# ============================================================
async def section_e(ck: Checker) -> None:
    section("E 指纹（内容改了会变、无关的东西变了不会变 —— 两侧都要验）")

    source_fp = compute_dataset_fingerprint(CASES)
    async with async_session_factory() as session:
        rows = list((await session.scalars(select(EvalCase))).all())
    db_fp = compute_dataset_fingerprint(rows)

    ck.check(db_fp == source_fp,
             "E1 源文件指纹 == 库中指纹（落库没丢信息、没被改）",
             f"源={source_fp[:16]}… 库={db_fp[:16]}…")
    print(f"  指纹 = {source_fp}")

    # E2 改一个字 → 必须变（否则指纹保护不了任何东西）
    tampered = [dict(c) for c in CASES]
    tampered[0]["reference"] = tampered[0]["reference"] + "。"
    ck.check(compute_dataset_fingerprint(tampered) != source_fp,
             "E2 改动参考答案一个字 → 指纹改变")

    # E3 顺序打乱 → 必须不变（否则每次查询顺序不同都会假报警）
    shuffled = list(reversed(CASES))
    ck.check(compute_dataset_fingerprint(shuffled) == source_fp,
             "E3 题目顺序打乱 → 指纹不变（顺序不是内容）")

    # E4 doc_ids（UUID）不进指纹 —— 重建语料会换 UUID，若算进去就会疯狂假报警
    fake_uuid = uuid.uuid4()
    with_uuid = [dict(c) for c in CASES]
    for case in with_uuid:
        case["doc_ids"] = [fake_uuid] if case["doc_slugs"] else []
    ck.check(compute_dataset_fingerprint(with_uuid) == source_fp,
             "E4 doc_ids（UUID）不进指纹（重建语料换了 UUID 也不该报改版）")

    # E5 id / created_at 不进指纹
    with_meta = [dict(c) for c in CASES]
    for index, case in enumerate(with_meta):
        case["id"] = 9000 + index
        case["created_at"] = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    ck.check(compute_dataset_fingerprint(with_meta) == source_fp,
             "E5 id / created_at 不进指纹（它们与题目内容无关）")

    # E6 doc_slugs 改动 → 必须变（依据文档变了，是实质改动）
    moved = [dict(c) for c in CASES]
    moved[0]["doc_slugs"] = ["finance-payment"]
    ck.check(compute_dataset_fingerprint(moved) != source_fp,
             "E6 改动 doc_slugs → 指纹改变")

    # E7 expected_tool 改动 → 必须变
    retooled = [dict(c) for c in CASES]
    retooled[40]["expected_tool"] = "current_time"
    ck.check(compute_dataset_fingerprint(retooled) != source_fp,
             "E7 改动 expected_tool → 指纹改变")


# ============================================================
# F HTTP 接口
# ============================================================
async def section_f(ck: Checker) -> None:
    section("F HTTP 接口（PRD §13.2 的 D21 验收点：/api/eval/cases ok）")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/eval/cases")
        ck.check(resp.status_code == 200, "F1 GET /api/eval/cases → 200",
                 f"实际={resp.status_code}")
        payload = resp.json() if resp.status_code == 200 else []
        ck.check(isinstance(payload, list) and len(payload) == EXPECTED_TOTAL,
                 f"F1 返回 {EXPECTED_TOTAL} 条", f"实际={len(payload)}")

        if payload:
            keys = set(payload[0])
            print(f"  列表返回字段 = {sorted(keys)}")
            ck.check("reference" not in keys and "evidence" not in keys,
                     "F2 列表**不含**参考答案与判定依据（防手滑，不是防攻击）")
            ck.check({"id", "case_key", "category", "question", "difficulty",
                      "is_negative"} <= keys,
                     "F2 列表含 PRD 要求的字段 + 标注字段")

        resp = await client.get("/api/eval/cases", params={"category": "doc_qa"})
        filtered = resp.json() if resp.status_code == 200 else []
        ck.check(len(filtered) == EXPECTED_BY_CATEGORY["doc_qa"],
                 f"F3 category=doc_qa 过滤生效（{EXPECTED_BY_CATEGORY['doc_qa']} 条）",
                 f"实际={len(filtered)}")

        # ★ 非法类别必须**报错**，不能静默返回空数组 ——
        #   返回 [] 会被读成"这个类别暂时没有题"，而实际是参数打错了
        resp = await client.get("/api/eval/cases", params={"category": "doc_qa_typo"})
        ck.check(resp.status_code == 422,
                 "F4 非法 category → 422（而不是静默返回空数组）",
                 f"实际={resp.status_code} body={resp.text[:80]}")

        resp = await client.get("/api/eval/cases", params={"limit": 10})
        ck.check(resp.status_code == 200 and len(resp.json()) == 10, "F5 分页 limit 生效")

        resp = await client.get("/api/eval/cases/A01")
        ck.check(resp.status_code == 200, "F6 GET /api/eval/cases/A01 → 200",
                 f"实际={resp.status_code}")
        detail = resp.json() if resp.status_code == 200 else {}
        ck.check(bool(detail.get("reference")) and bool(detail.get("evidence")),
                 "F6 详情接口返回参考答案与判定依据")

        resp = await client.get("/api/eval/cases/A999")
        ck.check(resp.status_code == 404, "F7 不存在的 case_key → 404",
                 f"实际={resp.status_code}")

        source_fp = compute_dataset_fingerprint(CASES)
        resp = await client.get("/api/eval/dataset")
        stats = resp.json() if resp.status_code == 200 else {}
        ck.check(stats.get("fingerprint") == source_fp,
                 "F8 /api/eval/dataset 的指纹与源文件一致（冻结有可对照的出口）",
                 f"接口={str(stats.get('fingerprint'))[:16]}… 源={source_fp[:16]}…")
        ck.check(stats.get("total") == EXPECTED_TOTAL,
                 "F8 数据集统计的条数正确", f"实际={stats.get('total')}")


# ============================================================
# G 覆盖与分布
# ============================================================
async def section_g(ck: Checker) -> None:
    section("G 覆盖与分布（语料有没有白写、难度有没有分层）")

    stats = summarize()
    print(f"  源文件统计 = {stats}")

    unused = set(SLUG_TO_FILENAME) - set(stats["covered_slugs"])
    ck.check(not unused, "G1 8 篇语料全部被题目引用（没有白写的文档）",
             f"未被引用={sorted(unused)}")

    by_difficulty = stats["by_difficulty"]
    levels = [k for k, v in by_difficulty.items() if v > 0]
    ck.check(len(levels) >= 2, "G2 难度至少分两层（否则分层抽样没有意义）",
             f"实际={by_difficulty}")
    print(f"  难度分布 = {by_difficulty}")

    # 抽样可行性：D22/D23 要按 (类别 × 难度) 分桶抽样，
    # 如果某个桶是空的，"分层抽样"就抽不出来 —— 这是能提前查的
    combos: dict[tuple[str, str], int] = {}
    for case in CASES:
        key = (case["category"], case["difficulty"])
        combos[key] = combos.get(key, 0) + 1
    print(f"  类别×难度分桶 = { {f'{k[0]}/{k[1]}': v for k, v in sorted(combos.items())} }")
    ck.check(len(combos) >= 5, "G3 类别×难度分桶数 ≥ 5（够分层抽样用）",
             f"实际桶数={len(combos)}")


async def main() -> int:
    ck = Checker()

    await section_a(ck)
    await section_b(ck)
    await section_c(ck)
    await section_d(ck)
    await section_e(ck)
    await section_f(ck)
    await section_g(ck)

    await engine.dispose()
    return ck.report()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
