"""
D21 补丁验证：删除作用域自限（delete scope）
==============================================
背景（2026-09-28）：
    D21 建了长期语料（8 篇 / 51 片）之后才发现，7 个历史验证脚本在调
    `delete_all_documents()` —— 跑一次全量回归就把评测集的依据删光，
    而**这些脚本的本意只是想清掉自己造的那两三篇**。

    危险点在于它完全静默：删除成功、退出码 0、断言全绿，
    只有"库里少了 8 篇文档"这一件事没人知道。

本补丁做了三件事：
    ① document_service 新增 `delete_documents_by_prefix()` /
       `count_documents_by_prefix()` —— 清理时作用域自限；
    ② `delete_all_documents()` 加硬闸（显式 confirm + 环境变量），
       误调时**报错**而不是照删；
    ③ 7 个脚本改用前缀删除，并且**跑完把自己的数据收干净**
       （只删别人的不够，留下自己的同样会污染 D22/D23 的检索评测）。

本脚本验证这三件事真的成立。跑法（项目根目录）：
    ~/.local/bin/uv run python -m scripts.d21_delete_scope_verify

分段：
    A 纯函数        —— 转义与"空前缀拒绝"（不连库）
    B 前缀删除行为  —— 真造数据真删，含 LIKE 通配符的误伤测试
    C 硬闸          —— 未授权时 delete_all_documents 必须**抛错**
    D 静态扫描      —— scripts/ 与 app/ 下不允许再有真实调用（用 AST，不看注释）
    E 长期语料安全  —— D21 的 8 篇语料仍在、内容没被动过

⚠ 本脚本会**写库**（B 段造数据、A/E 段只读），但只碰自己前缀的数据，
  跑完自清。E 段断言的就是"别人的数据没被碰"。
"""

import asyncio
import ast
import hashlib
import sys
from pathlib import Path

from sqlalchemy import select, text

from app.core.db import async_session_factory
from app.models.document import Document
from app.services.document_service import (
    DELETE_ALL_ENV_VAR,
    count_documents_by_prefix,
    create_document,
    delete_all_documents,
    delete_documents_by_prefix,
    escape_like,
)
from app.services.document_service import _prefix_pattern

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [PROJECT_ROOT / "scripts", PROJECT_ROOT / "app"]

# B 段用的临时前缀：与任何真实脚本的前缀都不冲突，且天然带下划线，
# 顺便把"LIKE 里的 _ 是不是被转义了"一起测了。
B_PREFIX = "zz-scope-probe"
B_UNDERSCORE_PREFIX = "zz_scope_probe"
B_CONTROL_PREFIX = "zz-scope-control"

# B 段只创建一个空文档行，不做 embedding（create_document 只插一行，很快）
B_TMP_DOCS = [
    f"{B_PREFIX}-a.md",
    f"{B_PREFIX}-b.md",
    f"{B_UNDERSCORE_PREFIX}-x.md",   # 带下划线：若未转义，前缀 "zz_scope_probe" 会误伤它
    f"{B_PREFIX.replace('-', 'X')}-x.md",  # zzXscopeXprobe-x.md：同样用于误伤测试
    f"{B_CONTROL_PREFIX}-stay.md",   # 对照：任何删除都不该碰到它
]


class Checker:
    """和 d21_eval_verify 同一套：分段报告 + 失败数作为退出码。"""

    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def check(self, condition: bool, label: str, detail: str = "") -> None:
        (self.passed if condition else self.failed).append(label)
        mark = "PASS" if condition else "FAIL"
        suffix = f"  —— {detail}" if detail else ""
        print(f"  [{mark}] {label}{suffix}")

    def report(self) -> int:
        total = len(self.passed) + len(self.failed)
        print()
        print("=" * 74)
        if self.failed:
            print(f"结果：{len(self.passed)}/{total} 通过，{len(self.failed)} 失败")
            for name in self.failed:
                print(f"  - {name}")
        else:
            print(f"结果：{total}/{total} 全部通过")
        print("=" * 74)
        return len(self.failed)


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ============================================================
# A 纯函数
# ============================================================
def section_a(ck: Checker) -> None:
    """不连库，先跑。纯函数错了一切都错。"""
    section("A 纯函数（LIKE 转义 / 空前缀拒绝）")

    ck.check(escape_like("d12") == "d12", "A1 无通配符时不加多余转义",
             f"实际={escape_like('d12')!r}")
    ck.check(escape_like("zz_scope") == "zz\\_scope", "A2 下划线被转义",
             f"实际={escape_like('zz_scope')!r}")
    ck.check(escape_like("a%b") == "a\\%b", "A3 百分号被转义",
             f"实际={escape_like('a%b')!r}")
    ck.check(escape_like("a\\b") == "a\\\\b", "A4 反斜杠本身被转义（否则转义符会吃掉后一个字符）",
             f"实际={escape_like('a\\b')!r}")
    ck.check(_prefix_pattern("d12") == "d12%", "A5 前缀被拼成 LIKE 模式",
             f"实际={_prefix_pattern('d12')!r}")

    # 空前缀 -> LIKE '%' -> 匹配全库。必须直接拒绝，不能"温和地"删光。
    for label, bad in (("A6 空前缀被拒绝", ""), ("A7 纯空白前缀被拒绝", "   ")):
        try:
            _prefix_pattern(bad)
        except ValueError as exc:
            ck.check(True, label, str(exc)[:40] + "…")
        else:
            ck.check(False, label, "居然没抛错 —— 这等于把危险函数换了个名字继续用")


# ============================================================
# B 前缀删除行为
# ============================================================
async def _seed_probe_docs() -> None:
    for filename in B_TMP_DOCS:
        await create_document(filename=filename, file_type="md")


async def _probe_counts() -> dict[str, int]:
    return {
        "main": await count_documents_by_prefix(B_PREFIX),
        "underscore": await count_documents_by_prefix(B_UNDERSCORE_PREFIX),
        "control": await count_documents_by_prefix(B_CONTROL_PREFIX),
    }


async def section_b(ck: Checker) -> None:
    section("B 前缀删除行为（真造数据真删）")

    # 先清干净可能的残留，让计数从确定状态出发
    for prefix in (B_PREFIX, B_UNDERSCORE_PREFIX, B_CONTROL_PREFIX):
        await delete_documents_by_prefix(prefix)

    await _seed_probe_docs()
    before = await _probe_counts()
    print(f"  建好 {len(B_TMP_DOCS)} 份探针文档：{B_TMP_DOCS}")
    print(f"  删除前计数 = {before}")

    ck.check(before["main"] == 2, "B1 主前缀命中 2 份", f"实际={before['main']}")
    ck.check(before["underscore"] == 1, "B2 下划线前缀命中 1 份", f"实际={before['underscore']}")
    ck.check(before["control"] == 1, "B3 对照前缀命中 1 份", f"实际={before['control']}")

    removed = await delete_documents_by_prefix(B_PREFIX)
    after = await _probe_counts()
    print(f"  delete_documents_by_prefix({B_PREFIX!r}) → 删除 {removed} 行")
    print(f"  删除后计数 = {after}")

    ck.check(removed == 2, "B4 返回的删除条数与命中数一致", f"返回={removed}")
    ck.check(after["main"] == 0, "B5 主前缀已清空", f"实际={after['main']}")
    ck.check(after["control"] == 1, "B6 对照数据没被碰（作用域自限的核心）",
             f"实际={after['control']}")
    ck.check(after["underscore"] == 1,
             "B7 前缀之间的包含关系没造成误删（zz-scope 不碰 zz_scope）",
             f"实际={after['underscore']}")

    # ---- LIKE 通配符误伤测试 ----
    # 若 _ 没被转义，前缀 "zz_scope_probe" 会匹配到 "zzXscopeXprobe-x.md"
    removed_u = await delete_documents_by_prefix(B_UNDERSCORE_PREFIX)
    left_all = await _probe_counts()
    # 现在只剩对照 + 那个 X 版（它不属于任何前缀）
    async with async_session_factory() as session:
        x_rows = (await session.scalars(
            select(Document).where(Document.filename == f"{B_PREFIX.replace('-', 'X')}-x.md")
        )).all()

    print(f"  delete_documents_by_prefix({B_UNDERSCORE_PREFIX!r}) → 删除 {removed_u} 行")
    ck.check(removed_u == 1,
             "B8 下划线前缀只删自己那 1 份（未转义的话会连 X 版一起删掉）",
             f"返回={removed_u}")
    ck.check(len(x_rows) == 1,
             "B9 X 版文档仍在（证明 _ 确实被转义成字面下划线）",
             f"实际={len(x_rows)}")

    # 收尾：把自己造的全清掉
    await delete_documents_by_prefix(B_PREFIX.replace("-", "X"))
    await delete_documents_by_prefix(B_CONTROL_PREFIX)
    final = await _probe_counts()
    ck.check(all(v == 0 for v in final.values()), "B10 探针数据已全部收干净", f"实际={final}")


# ============================================================
# C 硬闸
# ============================================================
async def section_c(ck: Checker) -> None:
    section("C 硬闸（未授权时 delete_all_documents 必须抛错）")

    import os

    if os.getenv(DELETE_ALL_ENV_VAR) == "1":
        print(f"  ⚠ 环境里已设 {DELETE_ALL_ENV_VAR}=1 —— 本节无法验证（会被判失败）")
        ck.check(False, "C0 前置：环境变量不应被设置",
                 f"{DELETE_ALL_ENV_VAR}=1 已经在环境里，硬闸被你自己打开了")
        return

    async with async_session_factory() as session:
        total_before = (await session.scalar(
            text("SELECT count(*) FROM documents")
        )) or 0

    try:
        await delete_all_documents()
    except RuntimeError as exc:
        message = str(exc)
        ck.check(True, "C1 未授权调用 → 抛 RuntimeError", message.splitlines()[0][:60] + "…")
        ck.check(DELETE_ALL_ENV_VAR in message,
                 "C2 报错信息里点了环境变量名（否则使用者不知道该怎么放行）")
        ck.check("delete_documents_by_prefix" in message,
                 "C3 报错信息里给了替代方案（否则只会让人去开闸）")
    else:
        ck.check(False, "C1 未授权调用 → 应抛 RuntimeError", "居然执行成功了")
        ck.check(False, "C2 报错信息里的环境变量名", "没有异常，无从验证")
        ck.check(False, "C3 报错信息里的替代方案", "没有异常，无从验证")

    async with async_session_factory() as session:
        total_after = (await session.scalar(
            text("SELECT count(*) FROM documents")
        )) or 0

    ck.check(total_before == total_after,
             "C4 抛错时**一行都没删**（拒绝执行 ≠ 删一半再报错）",
             f"之前={total_before} 之后={total_after}")
    ck.check(total_after > 0, "C5 库里确实还有文档（否则 C4 是空集合恒满足）",
             f"实际={total_after}")


# ============================================================
# D 静态扫描
# ============================================================
def find_delete_all_callers(dirs: list[Path], skip: Path) -> list[str]:
    """
    用 AST 找**真实的调用**，而不是文本匹配。

    为什么不能用 grep：我是把 `delete_all_documents` 写进注释和 docstring 来
    交代这次改动的原因的（比如"原先调 delete_all_documents() 清空全库"）。
    文本匹配会把那些说明当成违规调用，于是这个检查要么天天假报警，
    要么被迫放宽到形同虚设。AST 只看 Call 节点，注释和字符串一概不算。
    """
    hits: list[str] = []
    for directory in dirs:
        for path in sorted(directory.rglob("*.py")):
            if path == skip or "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                hits.append(f"{path}: 解析失败 {exc}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "id", None) or getattr(func, "attr", None)
                    if name == "delete_all_documents":
                        rel = path.relative_to(PROJECT_ROOT)
                        hits.append(f"{rel}:{node.lineno}")
    return hits


def section_d(ck: Checker) -> None:
    section("D 静态扫描（scripts/ 与 app/ 下不允许再有真实调用）")

    callers = find_delete_all_callers(SCAN_DIRS, skip=Path(__file__).resolve())
    print(f"  扫描目录 = {[str(d.relative_to(PROJECT_ROOT)) for d in SCAN_DIRS]}")
    if callers:
        for item in callers:
            print(f"    违规调用 → {item}")
    ck.check(not callers,
             "D1 没有任何文件调用 delete_all_documents（含 app/ 与 scripts/）",
             f"违规={callers}")

    # 元断言：扫描本身要有意义 —— 得确认它真的扫到了文件，
    # 否则"目录不存在 → 0 命中 → 全绿"就是空集合恒满足。
    scanned = sum(
        1 for directory in SCAN_DIRS
        for path in directory.rglob("*.py")
        if "__pycache__" not in path.parts
    )
    ck.check(scanned >= 20, "D2 扫描确实覆盖到了文件（防止扫了个空目录还报全绿）",
             f"扫描到 {scanned} 个 .py")


# ============================================================
# E 长期语料安全
# ============================================================
async def section_e(ck: Checker) -> None:
    section("E 长期语料安全（D21 的 8 篇还在、内容没被动过）")

    from scripts.d21_corpus import DOCUMENTS, SLUG_TO_FILENAME, full_text
    from app.services.retrieval_service import split_text

    filenames = list(SLUG_TO_FILENAME.values())
    async with async_session_factory() as session:
        rows = (await session.scalars(
            select(Document).where(Document.filename.in_(filenames))
        )).all()
    by_name = {row.filename: row for row in rows}

    print(f"  期望 {len(filenames)} 篇，实到 {len(rows)} 篇")
    missing = [name for name in filenames if name not in by_name]
    ck.check(len(rows) == len(filenames), "E1 8 篇语料仍在库里",
             f"缺失={missing}")

    total_chunks = sum(row.chunk_count for row in rows)
    ck.check(total_chunks > 20,
             "E2 切片总数 > 20（装得满重排候选窗口，说明语料是完整的）",
             f"实际={total_chunks}")

    # 逐篇核对切片数与"由源文件重新切一遍"的结果是否一致 ——
    # 这比"文档还在"更强：文档还在但内容被换掉，也是被改过。
    mismatched: list[str] = []
    for slug, filename, lines in DOCUMENTS:
        row = by_name.get(filename)
        if row is None:
            continue
        expected = len(split_text(full_text(lines)))
        if row.chunk_count != expected:
            mismatched.append(f"{slug}: 库={row.chunk_count} 源={expected}")
    ck.check(not mismatched,
             "E3 每篇的切片数与源文件重新切片的结果一致（内容没被换过）",
             f"不一致={mismatched}")


async def main() -> None:
    ck = Checker()

    section_a(ck)
    await section_b(ck)
    await section_c(ck)
    section_d(ck)
    await section_e(ck)

    failures = ck.report()
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
