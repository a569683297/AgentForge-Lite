"""
请求级引用收集器（D11）
========================
解决的问题：工具契约要求返回 str（LLM 只认文本），
但调用方（前端/API）还需要**结构化**的来源信息（source / content / similarity）。
格式化那一刻，结构化信息就再没有出口了 —— 「看起来像引用」和「真的是引用」的差别就在这。

方案（D11 选型 C）：
    工具仍然返回 str，但**额外**把格式化之前的原始结构登记到请求级收集器；
    run_agent 结束后取出，随回答一起返回。
    → LLM 走文本通道（读 [1] 编号），前端走结构通道（拿映射表），两者靠「编号」对齐。

    A. 正则反解 tool 消息   ❌ 格式耦合 / 换行边界难判 / 序列化再反序列化
    B. 工具返回 dict        ❌ 破坏 D10 的工具契约（LLM 读不了 Python 字面量）
    C. 请求级收集器         ✅ 契约不变、不解析文本   ← 本文件

前端类比：这就是 Node.js 的 AsyncLocalStorage（请求作用域存储）。
不用把它当参数一层层往下传，但它天然按请求隔离。

------------------------------------------------------------
⚠ 一条实测出来的铁律（写之前必须知道，否则会静默串数据）
------------------------------------------------------------
ContextVar 复制的是「绑定」，不是「对象内容」。

错误写法：在父层级绑定一个 list，再并发起子 task ——
    子 task 继承到的是**同一个 list 对象**，append 互相串。
    实测现象：两个并发请求各自看到 ['E-片段1', 'F-片段1', 'E-片段2', 'F-片段2']。

正确写法：绑定动作必须发生在**每个请求自己的 task 内部**（reset_sources 在入口调）。
    这样每个请求各持一份 list，实测互不污染。

另：get_collector 保留 default=None + 惰性绑定，
是为了「工具被单独调用（没走 run_agent）」时也不炸，属于双保险。
"""

import contextvars
import re
from typing import Any

# default=None：
#   没绑定过时取到 None → get/record 里惰性绑定一个新的
#   这样即便调用方忘了 reset，也不会因为 LookupError 崩掉
_sources: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "citation_sources", default=None
)


def reset_sources() -> None:
    """
    重新绑定一个全新的收集器 —— **必须在每个请求入口调用**（run_agent 开头）。

    为什么不能省：同一个进程会连续/并发处理多个请求，
    不重新绑定就会读到上一个请求残留的来源，或者与其他请求共享同一个 list。
    """
    _sources.set([])


def record_sources(results: list[dict[str, Any]]) -> int:
    """
    登记一批检索结果，返回「本批编号的起始偏移」。

    返回值是给格式化用的：一次对话里 Agent 可能查多次库，
    编号必须**全局递增**，否则两批结果都是 [1][2][3]，
    LLM 写「根据 [2]」时根本分不清指哪一批。

    Args:
        results: retrieval_service.search() 的原始返回（list[dict]）
    Returns:
        本批之前的已有条数。调用方用 `start=offset + 1` 开始编号。
    """
    buf = _sources.get()
    if buf is None:                     # 本 task 还没绑定（如工具被单独测试）→ 惰性绑定
        buf = []
        _sources.set(buf)

    offset = len(buf)                   # 已有条数 = 本批第一条的编号 - 1
    for item in results:
        buf.append(
            {
                "index": len(buf) + 1,  # 全局递增编号，与文本里的 [n] 一一对应
                "source": item.get("source") or "未知来源",
                "content": item.get("content") or "",
                "similarity": item.get("similarity"),
                # D12：页码（PDF 才有，其他格式为 None）。
                # 有了它，前端才能把 [1] 定位到「员工手册.pdf 第 3 页」而不只是文件名。
                "page_ref": item.get("page_ref"),
            }
        )
    return offset


def get_sources() -> list[dict[str, Any]]:
    """取出当前请求收集到的全部来源（复制一份，避免调用方误改收集器的内部列表）。"""
    return list(_sources.get() or [])


# ------------------------------------------------------------
# 引用校验（纯函数，放在这里是为了不依赖 config —— 方便单独测试）
# ------------------------------------------------------------
# 只匹配「纯数字方括号」，不会误伤 markdown 链接 [文字](url)（里面不是纯数字）
_CITATION_RE = re.compile(r"\[(\d+)\]")


def check_citations(answer: str, total: int) -> list[int]:
    """
    找出回答里「引用了、但来源表中不存在」的编号 —— 即 LLM 的越界引用。

    为什么必须有这一步：D11 要求 LLM 标注编号之后，它可能为了「显得有依据」
    而乱标一个 sources 里根本不存在的 [5]。这类错误靠人眼看回答很难发现。

    为什么只检测、不改写：改写回答会引入新的不确定性（改错了更难查）。
    先记录 + 日志告警，等 D17 评测体系量化之后，再决定要不要干预。

    Args:
        answer: LLM 生成的回答文本
        total: 本次请求收集到的来源条数
    Returns:
        越界编号的升序列表（正常情况为空列表）
    """
    used = {int(n) for n in _CITATION_RE.findall(answer)}
    return sorted(n for n in used if n < 1 or n > total)
