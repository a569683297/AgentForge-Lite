"""
D22 验证：评测器、明细表与聚合
================================
对应 PRD §13.2 的 D22 验收点两条：
    ① 单条可评分
    ② **明细表有行、按类别可聚合**

这两条都是"能做出来"而不是"能跑通"的验收点，所以这个脚本的重点在
**把'声称做到了'变成'可机检的事实'**：

    · "明细表有行"     → 不靠手敲 SQL 声称，而是自己跑一轮评测再数（C 段）
    · "按类别可聚合"   → 直接走 HTTP 端点拉聚合结果并验其自洽（E 段）
    · "单条可评分"     → 断言 judge 的原始分数被如实落库、可按题取回（C/E 段）

--------------------------------------------------------------------------
本脚本自己跑一轮**小规模**评测（约 1 分钟），而不是依赖库里已有数据
--------------------------------------------------------------------------
依赖"跑之前库里恰好有什么"是隐性依赖，会在别人机器上变成随机假失败
（D14 的注释里已经写过这条）。所以这里自给自足：
    category=tool_call, limit=4, generation_runs=2, judge_runs=3
    4 题 × 2 次生成 = 8 条明细，24 次打分
跑完默认**删掉自己建的那条 run**（按 run_id 删，作用域自限）。
加 `--keep` 可以保留下来看。

运行（必须在项目根目录）：
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d22_verify
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d22_verify --keep
"""

import asyncio
import sys

import app.models  # noqa: F401 —— 导入即注册全部模型
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.core.db import engine
from app.main import app
from app.models.eval import EVAL_CATEGORIES
from app.services import eval_runner, eval_service, failure_taxonomy

# 小规模实跑的参数（见模块 docstring 的说明）
MINI_CONFIG = "hybrid_rerank"
MINI_CATEGORY = "tool_call"
MINI_LIMIT = 4
MINI_GENERATION_RUNS = 2
MINI_JUDGE_RUNS = 3


class Checker:
    """极简断言收集器。返回失败数，作为进程退出码 —— 让 CI/回归循环能用退出码判。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def section(self, title: str) -> None:
        print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  [PASS] {label}" + (f"  （{detail}）" if detail else ""))
        else:
            self.failed.append(label)
            print(f"  [FAIL] {label}" + (f"  （{detail}）" if detail else ""))
        return bool(condition)

    def summary(self) -> int:
        total = self.passed + len(self.failed)
        print(f"\n{'=' * 70}")
        if self.failed:
            print(f"未通过：{len(self.failed)}/{total}")
            for label in self.failed:
                print(f"  - {label}")
        else:
            print(f"全部通过：{self.passed}/{total}")
        print("=" * 70)
        return len(self.failed)


# ============================================================
# A 段：库结构（直接查 information_schema，不用 ORM —— 独立来源）
# ============================================================
async def section_a(ck: Checker) -> None:
    ck.section("A. 明细表结构（独立于 ORM 的查库核对）")

    async with engine.connect() as conn:
        tables = {row.table_name for row in (await conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ))).all()}
        ck.check("eval_case_results" in tables, "A1 eval_case_results 表存在")

        columns = (await conn.execute(text(
            "SELECT column_name, data_type, udt_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='eval_case_results'"
        ))).all()
        col_map = {row.column_name: (row.data_type, row.udt_name) for row in columns}

        ck.check(len(col_map) == 17, "A2 明细表 17 列", f"实际 {len(col_map)} 列")
        ck.check(
            col_map.get("tool_calls") == ("jsonb", "jsonb"),
            "A3 tool_calls 是 jsonb",
            str(col_map.get("tool_calls")),
        )
        ck.check(
            col_map.get("passed") == ("boolean", "bool"),
            "A4 passed 是 boolean",
            str(col_map.get("passed")),
        )

        # 决策 3 里刻意**没建**的一列。把它钉住，防止后来有人"顺手补上"：
        # 建一个永远为 NULL 的列，会让 `SELECT sequence_ok` 看起来像
        # "实现了但都不通过"（同族：D14 的 JSON null 让 IS NULL 恒假）。
        ck.check(
            "sequence_ok" not in col_map,
            "A5 没有 sequence_ok 列（决策 3：无多步题，不建永远为 NULL 的列）",
        )

        fk = (await conn.execute(text("""
            SELECT rc.delete_rule
            FROM information_schema.referential_constraints rc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = rc.constraint_name
            WHERE kcu.table_name = 'eval_case_results' AND kcu.column_name = 'run_id'
        """))).all()
        ck.check(
            bool(fk) and fk[0].delete_rule == "CASCADE",
            "A6 run_id 外键 ON DELETE CASCADE（删 run 不留孤儿明细）",
            str([r.delete_rule for r in fk]),
        )


# ============================================================
# B 段：失败归因纯函数（无 IO，可反复验）
# ============================================================
def section_b(ck: Checker) -> None:
    ck.section("B. 失败归因纯函数（6 值枚举 + 优先级）")

    derive = failure_taxonomy.derive_case_outcome
    good = dict(correctness=5, faithfulness=5, completeness=5)

    # B1 确定性：同一份分数推导 10 次，结果必须完全一样。
    #    这是"可复算"的直接检验 —— 只要有一条规则依赖了分数以外的状态，
    #    这里就会红。（也是"能写成纯函数"这个判据的可执行版本。）
    outcomes = {derive(**good, expected_tool="search_documents",
                       actual_tools=["search_documents"]) for _ in range(10)}
    ck.check(len(outcomes) == 1, "B1 同一输入重复推导 10 次结果唯一", str(outcomes))

    # B2 分数缺失必须抛错，不能静默当成"答错"
    try:
        derive(correctness=None, faithfulness=5, completeness=5)
        ck.check(False, "B2 分数缺失时抛 ValueError（不静默当成答错）", "未抛错")
    except ValueError:
        ck.check(True, "B2 分数缺失时抛 ValueError（不静默当成答错）")

    ck.check(
        derive(**good, run_failed=True) == (False, "system_error"),
        "B3 未跑通优先于一切（即使分数看起来满分）",
        str(derive(**good, run_failed=True)),
    )
    ck.check(
        derive(correctness=5, faithfulness=1, completeness=5,
               expected_tool="current_time", actual_tools=[]) == (False, "tool_miss"),
        "B4 tool_miss 优先于 hallucination（根因优先于表现）",
    )
    ck.check(
        derive(**good, expected_tool="__none__", actual_tools=["search_documents"])
        == (False, "tool_miss"),
        "B5 __none__ 是'显式要求不调工具'，调了就错",
    )
    ck.check(
        derive(**good, expected_tool=None, actual_tools=["search_documents"]) == (True, None)
        and derive(**good, expected_tool=None, actual_tools=[]) == (True, None),
        "B6 expected_tool=None 表示'未约束'，调不调都算过（与 __none__ 语义不同）",
    )
    ck.check(
        derive(**good, expected_tool="current_time", actual_tools=["search_documents"])
        == (False, "tool_miss"),
        "B7 调了别的工具也算不满足（测的是'选对工具'，不是'有调工具'）",
    )
    ck.check(
        derive(correctness=2, faithfulness=1, completeness=4) == (False, "hallucination")
        and derive(correctness=2, faithfulness=4, completeness=1) == (False, "incomplete")
        and derive(correctness=2, faithfulness=4, completeness=4) == (False, "wrong_answer"),
        "B8 三个失败模式的判据互不重叠（1/2 → 编造；漏要点 → 不完整；其余 → 答错）",
    )
    ck.check(
        derive(**good)[1] is None,
        "B9 通过时 failure_reason 必须是 None（不是'未填'）",
    )
    ck.check(
        failure_taxonomy.describe("某个没见过的值") == "某个没见过的值",
        "B10 describe 对枚举外的值原样返回（不掩盖）",
    )


# ============================================================
# C/D 段：实跑一轮小规模评测
# ============================================================
async def section_cd(ck: Checker) -> int | None:
    ck.section("C. 实跑一轮小规模评测（4 题 × 2 次生成 × 3 次打分）")

    config_before = settings.retriever_config
    result = await eval_runner.run_evaluation(
        MINI_CONFIG,
        generation_runs=MINI_GENERATION_RUNS,
        runs_per_case=MINI_JUDGE_RUNS,
        judge_model=settings.judge_model,
        category=MINI_CATEGORY,
        limit=MINI_LIMIT,
        verbose=False,
    )
    run_id = result["run_id"]
    print(f"  （本次 run_id={run_id}）")

    # ---- 配置必须被恢复：run_evaluation 改了全局 settings 再改回来 ----
    # 这条断言防的是"跑完不恢复"——它不会报错，只会让**之后的手动对话**
    # 莫名其妙地用最后一档检索配置，而没有任何提示。
    ck.check(
        settings.retriever_config == config_before,
        "C1 跑完后 settings.retriever_config 已恢复原值",
        f"{config_before} → {settings.retriever_config}",
    )

    expected_rows = MINI_LIMIT * MINI_GENERATION_RUNS
    ck.check(result["rows_written"] == expected_rows,
             f"C2 明细行数 = 题数 × 生成次数 = {expected_rows}", str(result["rows_written"]))

    rows = await eval_service.list_case_results(run_id, limit=2000)
    # 从**库里读回来**的行做后续断言（不是 runner 内存里的那份）——
    # 这才能验"落库过程没丢信息"，而不是验"内存里的对象对不对"。
    ck.check(len(rows) == expected_rows, "C3 从库读回的明细行数一致", str(len(rows)))

    catalog = await eval_service.list_cases(limit=500)
    known_keys = {case.case_key: case for case in catalog}
    category_by_key = {case.case_key: case.category for case in catalog}

    ck.check(
        all(row.category in EVAL_CATEGORIES for row in rows),
        "C4 明细的 category 都在合法枚举内",
    )
    ck.check(
        all(row.case_key in known_keys for row in rows),
        "C5 明细的 case_key 都能在评测集里找到",
    )
    # C6 冗余列一致性：明细的 category 必须等于 eval_cases 的 category。
    #    这条是"冗余存一份"这个决定的代价所在，必须被钉住 ——
    #    否则按类别聚合出来的分布是错的，而没有任何报错。
    ck.check(
        all(row.category == category_by_key.get(row.case_key) for row in rows),
        "C6 明细的 category 与评测集一致（冗余列不漂移）",
    )

    indexes = sorted(row.generation_index for row in rows)
    ck.check(
        indexes == sorted([i for _ in range(MINI_LIMIT) for i in range(MINI_GENERATION_RUNS)]),
        "C7 generation_index 覆盖 0..N-1（粒度确实是'一次生成'）",
        str(indexes),
    )
    ck.check(
        all(row.answer for row in rows),
        "C8 每行都有答案文本",
    )
    ck.check(
        all(row.judge_runs == MINI_JUDGE_RUNS for row in rows),
        f"C9 judge_runs 都等于 {MINI_JUDGE_RUNS}（无调用失败）",
        str(sorted({row.judge_runs for row in rows})),
    )
    ck.check(
        all(isinstance(row.judge_raw, list) and len(row.judge_raw) == row.judge_runs for row in rows),
        "C10 judge_raw 是列表且长度等于 judge_runs（原始分数被如实保留）",
    )
    ck.check(
        all(isinstance(row.tool_calls, list) for row in rows),
        "C11 tool_calls 读回来是列表（不是字符串或 None）",
        str(sorted({type(row.tool_calls).__name__ for row in rows})),
    )

    # ---- D 段：SQL 层的两个"同一语义两种表示"检查（D14 的坑复现）----
    ck.section("D. SQL 层一致性与汇总对账")

    async with engine.connect() as conn:
        null_tool = await conn.scalar(text(
            "SELECT count(*) FROM eval_case_results WHERE run_id=:r AND tool_calls IS NULL"
        ), {"r": run_id})
        json_null_tool = await conn.scalar(text(
            "SELECT count(*) FROM eval_case_results "
            "WHERE run_id=:r AND tool_calls = 'null'::jsonb"
        ), {"r": run_id})
        ck.check(null_tool == 0, "D1 没有 tool_calls 为 SQL NULL 的行", str(null_tool))
        # 上面两条合起来 = D14 那个坑的复现检查：
        #   JSON 列默认把 None 存成 JSON 字面量 null → `IS NULL` 恒假 →
        #   "同一语义有两种表示"。用了 JSONB(none_as_null=True) 后两条都应为 0。
        ck.check(json_null_tool == 0, "D2 没有 tool_calls 为 JSON 字面量 null 的行", str(json_null_tool))

        bad_pass = await conn.scalar(text(
            "SELECT count(*) FROM eval_case_results "
            "WHERE run_id=:r AND passed = true AND failure_reason IS NOT NULL"
        ), {"r": run_id})
        bad_fail = await conn.scalar(text(
            "SELECT count(*) FROM eval_case_results "
            "WHERE run_id=:r AND passed = false AND failure_reason IS NULL"
        ), {"r": run_id})
        ck.check(bad_pass == 0, "D3 通过的行没有 failure_reason", str(bad_pass))
        ck.check(bad_fail == 0, "D4 失败的行一定有 failure_reason", str(bad_fail))

    # D5 "当场算的汇总" == "从库里重算的汇总"。
    #    两条路径给出同一个数字，才说明落库没丢信息（也是 C/D 段所有断言的分母可信的前提）。
    from_db = eval_service.summarize_results(rows)
    ck.check(
        from_db["accuracy"] == result["accuracy"],
        "D5 从库重算的准确率 == 跑完当场算的准确率",
        f"{from_db['accuracy']} vs {result['accuracy']}",
    )
    run = await eval_service.get_run(run_id)
    ck.check(
        run is not None and run.accuracy == result["accuracy"],
        "D6 eval_runs 表里落的准确率 == 当场算的",
        str(run.accuracy if run else None),
    )
    ck.check(
        from_db["total_cases"] == MINI_LIMIT,
        f"D7 汇总的题数 = {MINI_LIMIT}（分母按题算，不是按行）",
        str(from_db["total_cases"]),
    )

    by_category = await eval_service.aggregate_by_category(run_id)
    ck.check(
        sum(item["rows"] for item in by_category) == len(rows),
        "D8 按类别聚合的行数之和 == 明细总数（聚合没漏行）",
        f"{sum(i['rows'] for i in by_category)} vs {len(rows)}",
    )
    ck.check(
        sum(from_db["failure_breakdown"].values()) == sum(1 for row in rows if not row.passed),
        "D9 失败模式分布之和 == 失败行数",
    )
    # D10 两个指纹必须落在 run 上（少任何一个，这个分数都不能与别的 run 比）
    ck.check(
        bool(run and run.dataset_fingerprint and run.corpus_fingerprint),
        "D10 run 上同时记录了评测集指纹与语料指纹",
    )
    corpus_now, _ = await eval_service.corpus_fingerprint_from_db()
    ck.check(
        run is not None and run.corpus_fingerprint == corpus_now,
        "D11 语料指纹 == 当前库中实际值（说明语料没被换过）",
    )

    return run_id


# ============================================================
# E 段：HTTP 接口
# ============================================================
def section_e(ck: Checker, run_id: int) -> None:
    ck.section("E. HTTP 接口（/api/eval/runs 一族）")

    with TestClient(app) as client:
        resp = client.get("/api/eval/runs")
        ck.check(resp.status_code == 200, "E1 GET /api/eval/runs 200", str(resp.status_code))
        ids = [item["id"] for item in resp.json()] if resp.status_code == 200 else []
        ck.check(run_id in ids, "E2 运行列表里包含本次 run", str(ids[:5]))

        resp = client.get(f"/api/eval/runs/{run_id}")
        ok = resp.status_code == 200
        ck.check(ok, "E3 GET /api/eval/runs/{id} 200", str(resp.status_code))
        detail = resp.json() if ok else {}

        expected_rows = MINI_LIMIT * MINI_GENERATION_RUNS
        ck.check(detail.get("rows") == expected_rows,
                 f"E4 详情里的 rows = {expected_rows}（明细表有行）", str(detail.get("rows")))
        ck.check(len(detail.get("by_category", [])) >= 1,
                 "E5 详情里有按类别聚合（按类别可聚合）", str(detail.get("by_category")))
        ck.check(
            detail.get("dataset_fingerprint") and detail.get("corpus_fingerprint"),
            "E6 详情里两个指纹都有（可比性四件套齐全）",
        )
        ck.check(
            "generation_inconsistent" in detail,
            "E7 详情带生成不一致题数（D21 发现的生成侧抖动有出口）",
        )

        resp = client.get(f"/api/eval/runs/{run_id}/cases")
        cases = resp.json() if resp.status_code == 200 else []
        ck.check(resp.status_code == 200 and len(cases) == expected_rows,
                 f"E8 GET /runs/{{id}}/cases 返回 {expected_rows} 行明细",
                 str(len(cases)))
        ck.check(
            cases and all("failure_reason_label" in item for item in cases),
            "E9 明细带 failure_reason 的中文标签（展示用派生值，不落库）",
        )

        # 过滤：本次只跑了 tool_call，按 doc_qa 过滤必须是空 ——
        # 这是"过滤生效"的**负向证据**（只验正向的话，一个永远不过滤的实现也会全绿）
        resp = client.get(f"/api/eval/runs/{run_id}/cases", params={"category": "doc_qa"})
        ck.check(resp.status_code == 200 and resp.json() == [],
                 "E10 按 doc_qa 过滤返回空（过滤真的生效）",
                 str(len(resp.json()) if resp.status_code == 200 else resp.status_code))

        # 单条取回（含检索片段与原始分数）—— "单条可评分"的可读证据
        first_key = cases[0]["case_key"] if cases else "C01"
        resp = client.get(f"/api/eval/runs/{run_id}/results/{first_key}")
        ok = resp.status_code == 200
        ck.check(ok, f"E11 GET /runs/{{id}}/results/{first_key} 200", str(resp.status_code))
        if ok:
            items = resp.json()
            ck.check(
                len(items) == MINI_GENERATION_RUNS,
                f"E12 同一题的多次生成全部返回（{MINI_GENERATION_RUNS} 条）", str(len(items)),
            )
            ck.check(
                all("retrieved_chunks" in item and "judge_raw" in item for item in items),
                "E13 单条详情带检索片段与原始分数（可复现 / 可重判）",
            )

        resp = client.get(f"/api/eval/runs/{run_id}/results/ZZZ")
        ck.check(resp.status_code == 404, "E14 不存在的题 → 404（不是 200 空列表）", str(resp.status_code))
        resp = client.get("/api/eval/runs/99999999")
        ck.check(resp.status_code == 404, "E15 不存在的 run → 404", str(resp.status_code))
        resp = client.get("/api/eval/cases", params={"category": "bogus"})
        ck.check(resp.status_code == 422, "E16 非法类别 → 422（D21 的行为没被改坏）", str(resp.status_code))

        # 跨事件循环：TestClient 用自己的线程 + 自己的 loop，
        # 退出前必须把连接池关在**它自己的 loop** 里，
        # 否则下一段 asyncio.run 会拿到绑在死 loop 上的连接。
        client.portal.call(engine.dispose)


# ============================================================
# F 段：清理（按 run_id 删，作用域自限）
# ============================================================
async def section_f(ck: Checker, run_id: int, keep: bool) -> None:
    ck.section("F. 清理与回归保护")

    if keep:
        print(f"  [--keep] 保留 run_id={run_id}，跳过清理断言")
        ck.check(True, "F1 --keep 模式：跳过清理")
        ck.check(True, "F2 --keep 模式：跳过清理")
        ck.check(True, "F3 --keep 模式：跳过清理")
        return

    corpus_before, chunks_before = await eval_service.corpus_fingerprint_from_db()
    deleted_runs, deleted_rows = await eval_service.delete_run(run_id)
    ck.check(deleted_runs == 1, "F1 删掉 1 条 run", str(deleted_runs))
    ck.check(deleted_rows > 0, "F2 明细被级联带走（不是留在库里当孤儿）", str(deleted_rows))

    run = await eval_service.get_run(run_id)
    ck.check(run is None, "F3 删后 run 查不到")
    remaining = await eval_service.count_case_results(run_id)
    ck.check(remaining == 0, "F4 删后该 run 的明细为 0", str(remaining))

    # F5 清理必须**只动自己的数据**：语料一片不少。
    #    这是 D21 那次"跑一次回归删光 51 片语料"事故的一般化检查 ——
    #    任何清理动作之后都要问一句"它还动了什么"。
    corpus_after, chunks_after = await eval_service.corpus_fingerprint_from_db()
    ck.check(
        corpus_before == corpus_after and chunks_before == chunks_after,
        "F5 清理没碰到语料（指纹与切片数与清理前一致）",
        f"{chunks_before} → {chunks_after} 片",
    )
    # F6 评测集也一片不少
    _, case_count = await eval_service.fingerprint_from_db()
    ck.check(case_count == 50, "F6 清理没碰到评测集（仍是 50 条）", str(case_count))


# ============================================================
# 主流程
# ============================================================
async def service_layer(ck: Checker) -> int:
    await section_a(ck)
    section_b(ck)
    run_id = await section_cd(ck)
    # 本 loop 用完就把连接池关掉：下一段 TestClient 是另一个 loop，
    # 不关的话它会拿到绑在**这个已结束的 loop** 上的连接 → `Event loop is closed`
    await engine.dispose()
    if run_id is None:
        raise RuntimeError("C/D 段没有产出 run_id，后续 HTTP 断言无法进行")
    return run_id


def main() -> None:
    keep = "--keep" in sys.argv
    ck = Checker()

    run_id = asyncio.run(service_layer(ck))
    section_e(ck, run_id)
    asyncio.run(section_f(ck, run_id, keep))

    failed = ck.summary()
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
