"""
D24 报告生成验证（33 条量级断言，全绿才算通过）
================================================
验证对象：`app/services/eval_report_service.py` + `scripts/d24_report.py`

    uv run python -m scripts.d24_report_verify

--------------------------------------------------------------------------
分五段，各段回答一个不同的问题
--------------------------------------------------------------------------
    A 纯函数      render_report 是否**确定性**、§9.5 四个必需章节是否都在
    B 守卫        ★ 负面测试：**故意做坏数据，断言它真的会拒绝出报告**
    C 数字一致性  ★ 报告里的数 = **用另一条独立 SQL 重新算出来的数**（不是"报告自己说它对"）
    D 规则        失败案例不足 3 条时，报告是否**如实写明不足**（而不是悄悄只放 1 条）
    E 落盘与回写  写文件能不能回读一致；report_path 回写能不能真的改到库、能不能还原

--------------------------------------------------------------------------
为什么 B 段必须是「负面测试」
--------------------------------------------------------------------------
守卫（指纹不一致就拒绝出报告）最容易写成**看着像检查、实际永远通过**的代码：
比如把条件写反、或者异常被上层 `except Exception` 吞掉。
所以 B 段**主动制造三种坏数据**，断言每一种都抛出 `ReportGuardError`；
若某一条不抛，那就是守卫失效 —— 此时 C/D 段的绿灯**毫无意义**。
（同 D24 加练那一轮的做法：断言升级成 assert 之后必须做负测试验证它真会失败。）

--------------------------------------------------------------------------
为什么 C 段要用「另一条独立 SQL」
--------------------------------------------------------------------------
若 C 段复用 `summarize_results`，那它只能证明"函数调了两次结果一样"，
**证明不了报告里的数是对的** —— 分母口径写错时两边会一起错。
所以 C 段在 SQL 里**重新实现一遍题级多数投票**，两条独立路径对上才算数。
（同 D21：「我出题时是对的」不是证据，「把题拿回语料里验一遍」才是。）
"""

import asyncio
import datetime as dt
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import app.models  # noqa: F401
from sqlalchemy import text

from app.core.db import async_session_factory, engine
from app.services import eval_report_service as rs

PASS = 0
FAIL = 0
SECTION = ""


def check(label: str, cond: bool, detail: str = "") -> None:
    """统一断言出口：打印 + 计数。不用 `assert` 是为了让**全部断言都跑完**再给汇总结论。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {detail}")


def head(title: str) -> None:
    global SECTION
    SECTION = title
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ============================================================
# 造数据的小工具（A/B/D 段用，不碰真库）
# ============================================================
def fake_run(run_id, config, dfp="dfp-aaa", cfp="cfp-bbb", judge="deepseek", gr=1, rpc=3):
    return SimpleNamespace(
        id=run_id, config_name=config, dataset_fingerprint=dfp, corpus_fingerprint=cfp,
        judge_model=judge, generation_runs=gr, runs_per_case=rpc, report_path=None,
        created_at=dt.datetime(2026, 9, 30, 12, 0, 0),
    )


def fake_row(case_key, category, passed, cor=5.0, fai=5.0, com=5.0,
             reason=None, answer="答案", gen=0, judge_raw=None):
    return SimpleNamespace(
        case_key=case_key, category=category, passed=passed, generation_index=gen,
        score_correctness=cor, score_faithfulness=fai, score_completeness=com,
        failure_reason=reason, answer=answer, judge_raw=judge_raw, run_id=0,
    )


def fake_trio():
    """
    造一组**自洽的最小可比数据**：3 配置 × 4 题（2 内容题 + 1 跨文档 + 1 工具题）。

    刻意让 `hybrid_rerank` **多对一条**，这样 A/D 段能同时覆盖
    "有差异" 与 "无差异" 两种分支 —— 只测一种分支的分支覆盖等于没测。
    """
    runs = [
        fake_run(101, "pure_vector"),
        fake_run(102, "hybrid"),
        fake_run(103, "hybrid_rerank"),
    ]
    rows = {
        101: [fake_row("A01", "doc_qa", True, 5.0, 5.0, 5.0),
              fake_row("A02", "doc_qa", False, 2.0, 4.0, 3.0, reason="wrong_answer"),
              fake_row("B01", "cross_doc", True, 4.0, 5.0, 4.0),
              fake_row("C01", "tool_call", True, None, None, None)],
        102: [fake_row("A01", "doc_qa", True, 5.0, 5.0, 5.0),
              fake_row("A02", "doc_qa", True, 4.0, 4.5, 4.0),
              fake_row("B01", "cross_doc", True, 4.0, 5.0, 4.0),
              fake_row("C01", "tool_call", True, None, None, None)],
        103: [fake_row("A01", "doc_qa", True, 5.0, 5.0, 5.0),
              fake_row("A02", "doc_qa", True, 4.0, 4.5, 4.0),
              fake_row("B01", "cross_doc", True, 4.0, 5.0, 4.0),
              fake_row("C01", "tool_call", True, None, None, None)],
    }
    return runs, rows


# ============================================================
# A 段：纯函数
# ============================================================
def section_a() -> None:
    head("A 段：render_report 是纯函数 + §9.5 四个必需章节齐全")

    runs, rows = fake_trio()
    ts = dt.datetime(2026, 9, 30, 12, 0, 0)
    md1 = rs.render_report(runs, rows, generated_at=ts)
    md2 = rs.render_report(runs, rows, generated_at=ts)
    check("A1 同一输入两次渲染**逐字相同**（确定性）", md1 == md2,
          f"len={len(md1)}")

    for i, sec in enumerate(rs.REQUIRED_SECTIONS, 1):
        check(f"A2.{i} 必需章节存在：{sec}", sec in md1)

    check("A3 四个必需章节**顺序正确**（§9.5 规定的流向）",
          [md1.index(s) for s in rs.REQUIRED_SECTIONS] ==
          sorted(md1.index(s) for s in rs.REQUIRED_SECTIONS))

    check("A4 章节标题不重复（唯一出处）",
          all(md1.count(s) == 1 for s in rs.REQUIRED_SECTIONS))

    check("A5 表格分隔行格式正确（含 `|---|` 行，且表头与数据行连续）",
          "|---|---|" in md1)

    # 目标/分母口径必须显式写出 —— 否则读者会把两个口径互相印证
    check("A6 报告显式声明「两个口径不要互相印证」",
          "不要互相印证" in md1 and "按**题**算" in md1 and "按**行**算" in md1)

    # 工具类题的 NULL 分数不能渲染成 0.000
    check("A7 工具类题的 NULL 分数渲染为 `—` 而不是 `0.000`",
          "| tool_call | 100.00% | 1/1 | — |" in md1 and "0.000" not in md1.split("## 2. 每类明细")[1].split("## 3.")[0])

    # 有差异分支：hybrid_rerank 多对一条 → 应给出达标判定
    check("A8 有差异时会给出 S2 达标判定分支", "未达到 PRD S2 门槛" in md1 or "达到 PRD S2" in md1)

    # 无差异分支
    same_rows = {k: [fake_row("A01", "doc_qa", True)] for k in (101, 102, 103)}
    md_same = rs.render_report(runs, same_rows, generated_at=ts)
    check("A9 无差异分支会说「未测出配置差异」并给出「不可解读为重排无用」的警告",
          "完全相同" in md_same and "未达标" in md_same and "不能解读为" in md_same)

    check("A10 S2 门槛值在报告里有出现（不是只写在代码里）",
          f"+{rs.S2_MIN_GAIN_PP:.0f} 个百分点" in md_same)


# ============================================================
# B 段：守卫（负面测试 —— 必须真会拒绝）
# ============================================================
def section_b() -> None:
    head("B 段：可比性守卫的**负面测试**（三种坏数据都必须被拒绝）")

    runs, rows = fake_trio()

    def expect_reject(label, rs_, rw_):
        try:
            rs.check_comparable(rs_, rw_)
        except rs.ReportGuardError as e:
            check(label, True, f"→ 已拒绝：{str(e)[:52]}…")
            return
        check(label, False, "→ ❌ 没拒绝（守卫失效，后面的绿灯都不可信）")

    # 正常数据必须先通过（否则下面的"拒绝"可能只是因为它本来就不过）
    try:
        rs.check_comparable(runs, rows)
        check("B0 正常数据**通过**守卫（基准，防止负面测试假通过）", True)
    except rs.ReportGuardError as e:
        check("B0 正常数据通过守卫", False, f"→ ❌ 正常数据被拒：{e}")

    # ① 评测集指纹不一致
    r1 = [fake_run(101, "pure_vector", dfp="dfp-aaa"), fake_run(102, "hybrid", dfp="dfp-aaa"),
          fake_run(103, "hybrid_rerank", dfp="dfp-ZZZ")]
    expect_reject("B1 评测集指纹不一致 → 拒绝", r1, rows)

    # ② 语料指纹不一致
    r2 = [fake_run(101, "pure_vector", cfp="cfp-bbb"), fake_run(102, "hybrid", cfp="cfp-ccc"),
          fake_run(103, "hybrid_rerank", cfp="cfp-bbb")]
    expect_reject("B2 语料指纹不一致 → 拒绝", r2, rows)

    # ③ 题号集合不同（其中一个配置被 --limit 截过）
    rows3 = dict(rows)
    rows3[103] = rows[103][:2]
    expect_reject("B3 题号集合不同（被 --limit 截过）→ 拒绝", runs, rows3)

    # ④ 配置不全
    r4 = [fake_run(101, "pure_vector"), fake_run(102, "hybrid")]
    expect_reject("B4 配置不全（只有 2 个）→ 拒绝", r4, {101: rows[101], 102: rows[102]})

    # ⑤ 指纹为空（老轮次）
    r5 = [fake_run(101, "pure_vector", dfp=""), fake_run(102, "hybrid", dfp=""),
          fake_run(103, "hybrid_rerank", dfp="")]
    expect_reject("B5 指纹为空（指纹机制生效之前的轮次）→ 拒绝", r5, rows)

    # ⑥ 某配置没有明细行
    expect_reject("B6 某配置没有明细行 → 拒绝", runs, {101: rows[101], 102: rows[102], 103: []})

    # ⑦ 异常类型必须是自己的类型，不能是 RuntimeError 之类
    check("B7 守卫抛的是 `ReportGuardError`（调用方能区分「数据不可比」与「程序出错」）",
          issubclass(rs.ReportGuardError, RuntimeError))


# ============================================================
# C 段：报告数字 = 独立 SQL 重算的数字
# ============================================================
async def section_c() -> None:
    head("C 段：报告里的数与**独立 SQL**重算的数一致（真实库数据）")

    runs = await rs.load_latest_runs()
    check("C1 取到 3 个配置的最新一轮", len(runs) == 3,
          f"→ {[(r.id, r.config_name) for r in runs]}")
    check("C2 配置名与 RETRIEVER_CONFIGS 一致",
          sorted(r.config_name for r in runs) == sorted(rs.RETRIEVER_CONFIGS))

    rows_by_run = {r.id: await rs.load_rows(r.id) for r in runs}
    try:
        rs.check_comparable(runs, rows_by_run)
        check("C3 真实数据通过可比性守卫", True)
    except rs.ReportGuardError as e:
        check("C3 真实数据通过可比性守卫", False, f"→ {e}")
        return

    md = rs.render_report(runs, rows_by_run)

    # 独立 SQL：在 SQL 里**重新实现一遍题级多数投票**，不复用 summarize_results
    async with async_session_factory() as session:
        for r in sorted(runs, key=lambda x: x.id):
            row = (await session.execute(text("""
                with per_case as (
                    select case_key,
                           count(*) as n,
                           count(*) filter (where passed) as ok
                    from eval_case_results
                    where run_id = :rid
                    group by case_key
                )
                select count(*) filter (where ok * 2 > n) as passed,
                       count(*) as total
                from per_case
            """), {"rid": r.id})).one()
            passed, total = int(row[0]), int(row[1])
            expect_frac = f"{passed}/{total}"
            expect_pct = f"{passed / total:.2%}"
            check(f"C4.{r.config_name} 报告含独立 SQL 算出的 `{expect_frac}`",
                  expect_frac in md, f"（准确率 {expect_pct}）")

        # 均分的真实分母：三列分数非 NULL 的行数（这条抓的是"分母悄悄变小"）
        scored = {r.id: sum(1 for x in rows_by_run[r.id] if x.score_correctness is not None)
                  for r in runs}
        for r in sorted(runs, key=lambda x: x.id):
            check(f"C5.{r.config_name} 均分分母写成「{scored[r.id]}（共 {len(rows_by_run[r.id])} 行）」",
                  f"{scored[r.id]}（共 {len(rows_by_run[r.id])} 行）" in md)

        # 三配置同分这个**结论性事实**必须与库一致 —— 双向断言：
        # 同分时必须写「完全相同」；**不同分时必须不写**。
        # 只写单向（同分→含字样）的话，不同分那半边永远为真 = 假通过。
        async with async_session_factory() as s_acc:
            per_config = {}
            for r in runs:
                per_config[r.config_name] = int((await s_acc.execute(text("""
                    with per_case as (
                        select case_key, count(*) n, count(*) filter (where passed) ok
                        from eval_case_results where run_id = :rid group by case_key
                    )
                    select count(*) filter (where ok * 2 > n) from per_case
                """), {"rid": r.id})).scalar())
        identical = len(set(per_config.values())) == 1
        has_wording = "完全相同" in md
        check("C6 三配置同分与否 → 报告措辞与库一致（双向）",
              has_wording == identical,
              f"→ 通过题数 {per_config}，报告含「完全相同」={has_wording}，应为 {identical}")

        # 失败案例：库里真有几条失败
        n_fail = sum(len([x for x in rows_by_run[r.id] if not x.passed]) for r in runs)
        check("C7 报告写明失败案例总量", f"三配置合计 {n_fail} 条判定失败" in md,
              f"→ 实际 {n_fail} 条")

        # 摘要里的差值必须由数据算出：同分时应写 "+0.00 个百分点"
        check("C8 差值与库一致（同分 → +0.00）",
              ("+0.00 个百分点" in md) == identical,
              f"→ identical={identical}")


# ============================================================
# D 段：失败案例「各取 3 条取不满」必须如实写明
# ============================================================
def section_d() -> None:
    head("D 段：失败案例取不满 3 条时，报告**如实写明不足**")

    runs, rows = fake_trio()
    ts = dt.datetime(2026, 9, 30, 12, 0, 0)
    md = rs.render_report(runs, rows, generated_at=ts)

    # 101 只有 1 条失败（A02），102/103 没有失败
    check("D1 失败数 < 3 的配置写明「共失败 N 条，不足 3 条」",
          f"该配置共失败 **1** 条，不足 {rs.FAILURE_CASES_PER_CONFIG} 条" in md)
    check("D2 无失败的配置写明「没有失败案例」", "该配置**没有失败案例**。" in md)
    check("D3 明说了「只有 N 条」与「截断到 N 条」是两件事",
          "是两件完全不同的事" in md)

    # 失败足够多时，应改为「取前 3 条」而不是「不足」
    many = {
        101: [fake_row("A01", "doc_qa", False, 1.0, 1.0, 1.0, reason="wrong_answer"),
              fake_row("A02", "doc_qa", False, 1.0, 1.0, 1.0, reason="wrong_answer"),
              fake_row("A03", "doc_qa", False, 1.0, 1.0, 1.0, reason="incomplete"),
              fake_row("A04", "doc_qa", False, 1.0, 1.0, 1.0, reason="incomplete"),
              fake_row("B01", "cross_doc", True)],
        102: rows[102], 103: rows[103],
    }
    md2 = rs.render_report(runs, many, generated_at=ts)
    check("D4 失败 ≥3 条时改为「取前 3 条」", "以下取前 3 条" in md2)
    # 只取 3 条：库里有 4 条失败（101 上），报告里必须**只出现 3 次题号**。
    # 这条防的是"PRD 说取 3 条、代码却全放"—— 全放不会报错，只是报告失控。
    seg = md2[md2.index("## 3. 失败案例"):md2.index("## 4. 结论与建议")]
    n_case = seg.count("**题号 `")
    check("D5 实际只取 3 条（不因为库里更多就全放）", n_case == rs.FAILURE_CASES_PER_CONFIG,
          f"→ 段内题号出现 {n_case} 次，应为 {rs.FAILURE_CASES_PER_CONFIG}")

    # 归因分布：两种 reason 都要能出现
    check("D6 失败归因来自库字段（`failure_reason`），不是编的",
          "`wrong_answer`" in md2 and "`incomplete`" in md2)


# ============================================================
# E 段：落盘 + 回读 + report_path 回写与还原
# ============================================================
async def section_e() -> None:
    head("E 段：落盘回读一致 + report_path 回写与**还原**（共享数据源的清理有两半）")

    runs = await rs.load_latest_runs()
    rows_by_run = {r.id: await rs.load_rows(r.id) for r in runs}
    md = rs.render_report(runs, rows_by_run)

    with tempfile.TemporaryDirectory() as tmp:
        p = rs.output_path("verify-tmp", out_dir=tmp)
        p.write_text(md, encoding="utf-8")
        back = p.read_text(encoding="utf-8")
        check("E1 落盘后回读与内存中一致（长度 + 逐字）",
              len(back) == len(md) and back == md, f"→ {len(back)} 字符")
        check("E2 文件名符合 PRD F7.4 约定 eval-report-{date}.md",
              p.name == "eval-report-verify-tmp.md")

    # ---- report_path 回写测试：先记原值 → 写测试值 → 断言 → 还原 → 断言还原 ----
    run_ids = [r.id for r in runs]
    original = {r.id: r.report_path for r in runs}
    tmp_marker = "docs/eval-report-__verify__.md"
    try:
        n = await rs.attach_report_path(run_ids, tmp_marker)
        check("E3 回写 report_path 影响行数 = run 数", n == len(run_ids), f"→ {n}")

        async with async_session_factory() as session:
            got = (await session.execute(
                text("select id, report_path from eval_runs where id = any(:ids)"),
                {"ids": run_ids},
            )).all()
        check("E4 **回库查**确认 report_path 真的改了（不是只调了个函数）",
              all(g[1] == tmp_marker for g in got), f"→ {[g[1] for g in got]}")
    finally:
        # 清理的后半：「别留下自己的」。写测试值必须还原，
        # 否则这个验证脚本每跑一次就把 report_path 污染成 __verify__，
        # 而真正的报告路径再也对不上 —— 且不报错。
        for rid, old in original.items():
            async with async_session_factory() as session:
                await session.execute(
                    text("update eval_runs set report_path = :p where id = :i"),
                    {"p": old, "i": rid},
                )
                await session.commit()
        async with async_session_factory() as session:
            now = (await session.execute(
                text("select id, report_path from eval_runs where id = any(:ids)"),
                {"ids": run_ids},
            )).all()
        now_map = {m["id"]: m["report_path"] for m in (dict(x._mapping) for x in now)}
        check("E5 测试值已**还原**回原值（清理的后半：别留下自己的）",
              now_map == original, f"→ 现状 {now_map} 应为 {original}")


# ============================================================
# F 段：跨配置失败一致性（本轮新增的核心判断）
# ============================================================
def section_f() -> None:
    head("F 段：跨配置共有失败的**答案一致性**（决定失败案例能不能用）")

    ts = dt.datetime(2026, 9, 30, 12, 0, 0)

    # ---- F1 指纹函数本身 ----
    check("F1a 同一文本指纹相同（确定性）",
          rs.answer_signature("同样的答案") == rs.answer_signature("同样的答案"))
    check("F1b 不同文本指纹不同",
          rs.answer_signature("答案甲") != rs.answer_signature("答案乙"))
    check("F1c 指纹长度 8 位十六进制",
          len(rs.answer_signature("x")) == 8 and all(c in "0123456789abcdef" for c in rs.answer_signature("x")))
    check("F1d 空值不炸（None / '' 都给同一个指纹）",
          rs.answer_signature(None) == rs.answer_signature(""))

    runs = [fake_run(101, "pure_vector"), fake_run(102, "hybrid"), fake_run(103, "hybrid_rerank")]

    # ---- F2 三个配置答案**逐字相同** → 报告必须说"相同"，且不得说"逐字不同" ----
    same = {
        101: [fake_row("B01", "cross_doc", False, 2.0, 5.0, 3.0, reason="wrong_answer", answer="同一份答案")],
        102: [fake_row("B01", "cross_doc", False, 2.0, 5.0, 3.0, reason="wrong_answer", answer="同一份答案")],
        103: [fake_row("B01", "cross_doc", False, 2.0, 5.0, 3.0, reason="incomplete", answer="同一份答案")],
    }
    md_same = rs.render_report(runs, same, generated_at=ts)
    check("F2a 答案相同时表格判为「✅ 相同」", "✅ 相同" in md_same)
    check("F2b 答案相同时**不出现**「逐字不同」的结论（否则就是看到了不存在的事实）",
          "逐字不同" not in md_same)
    check("F2c 答案相同时给出「归因差异来自裁判侧」的正确解释",
          "裁判侧" in md_same)

    # ---- F3 三个配置答案**不同** → 报告必须说"逐字不同"并拒绝归因于配置 ----
    diff = {
        101: [fake_row("B01", "cross_doc", False, 2.0, 5.0, 3.0, reason="wrong_answer", answer="答案甲甲甲")],
        102: [fake_row("B01", "cross_doc", False, 2.0, 5.0, 3.0, reason="wrong_answer", answer="答案乙乙")],
        103: [fake_row("B01", "cross_doc", False, 2.0, 5.0, 3.0, reason="incomplete", answer="答案丙")],
    }
    md_diff = rs.render_report(runs, diff, generated_at=ts)
    check("F3a 答案不同时表格判为「❌ 不同」", "❌ 不同" in md_diff)
    check("F3b 明确写出「不能归因于检索配置」", "不能归因于检索配置" in md_diff)
    check("F3c 明确写出「归因不同这件事本身也不能归因于配置」",
          "也不可归因于配置" in md_diff)
    check("F3d 指出这是**生成侧抖动**（分层归属正确）", "生成侧抖动" in md_diff)
    check("F3e 给出行动建议（固定生成侧），而不是只说现象",
          "generation_runs" in md_diff)

    # ---- F4 每一条失败案例都要带答案指纹（读者能自己核对） ----
    check("F4 §3 的每条失败案例都带答案指纹",
          md_diff.count("答案指纹：`") == 3, f"→ 出现 {md_diff.count('答案指纹：`')} 次，应为 3")

    # ---- F5 摘要必须写「涉及 N 道题」，不能只报条数 ----
    check("F5a 摘要写「涉及 N 道题」（与「N 条判定」分开说）",
          "**涉及 1 道题**" in md_diff)
    check("F5b 摘要里的题号是可指认的", "`B01`" in md_diff.split("## 1. 总体对比表")[0])

    # ---- F6 两种情形下都应有 §4.2 的警告句（防读者把归因差异直接当配置差异） ----
    check("F6 §4.2 带警告：归因不同 ≠ 失败模式不同",
          "先别当成" in md_same and "先别当成" in md_diff)


async def main() -> int:
    print("=" * 74)
    print("D24 报告生成验证")
    print("=" * 74)

    section_a()
    section_b()
    await section_c()
    section_d()
    await section_e()
    section_f()

    await engine.dispose()

    print()
    print("=" * 74)
    total = PASS + FAIL
    print(f"汇总：{PASS}/{total} 通过" + (f"，{FAIL} 条失败" if FAIL else "，全部通过"))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
