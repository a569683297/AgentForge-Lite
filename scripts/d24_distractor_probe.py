"""
D24 干扰文档探针：给语料临时插入「同主题、不同结论」的文档，量 gold 名次掉多少
================================================================================
**零 LLM 调用 / 零 token**（embedding 与重排都是本地模型）。

--------------------------------------------------------------------------
这个探针要回答的唯一问题
--------------------------------------------------------------------------
D24 的甲案（改问法）实测失败：10 条挑战题里 **9 条 gold 仍是第 1 名**。
原因是语义嵌入对口语化改写几乎免疫 —— 语料里只有一篇讲差旅的文档时，
你把它说成「去武汉出差住酒店最多能报多少」，它照样精准命中。

→ 结论指向乙案：**竞争必须在语料侧造**。本探针就是给乙案去风险的零成本模拟。

--------------------------------------------------------------------------
为什么必须做「剂量-反应」，而不是只插一篇
--------------------------------------------------------------------------
gold 名次是**文档级**的（第一篇 gold 切片排第几）。
插一篇同主题干扰篇，它的切片会插到 gold 前面 —— 但**能插进几条**取决于
干扰篇里有几片和问题同样贴近。一篇 1500 字的干扰篇，与问题强相关的一般只有
1~2 条条款对应的切片，所以：

    一篇干扰篇 ≈ 把 gold 从第 1 名挤到第 2~3 名   → **推不出 top5**

而 B 配置（hybrid）取融合序前 5 条，gold 必须在 6~20 名区间才可能
出现「B 拿不到、C 的重排窗口 20 还能救回来」——那才是唯一有区分度的区间。

所以本探针按 1→2→3→4 篇**逐篇追加**，每加一篇量一次，
得到一条曲线。它直接回答乙案真正的决策问题：**要写几篇才值得写**。

--------------------------------------------------------------------------
三件必须做对的事（否则结论不可用）
--------------------------------------------------------------------------
① **干扰篇走生产同一条入库路径**：`ingest_texts(["\n".join(条款)], filename)`，
   与 `scripts/d21_seed.py` 逐字相同的调用形态（切片 / 分词 / 向量化全由
   `document_service._ingest_segments` 负责）。手写 INSERT 会造出一个
   切片边界与真实语料不同的假文档，测出来的名次不可比。

② **必须同时记录「干扰篇自己的名次」**：
   如果干扰篇压根排不上来，那是「干扰篇写得不够像」（可修）；
   如果干扰篇排上来了、gold 却纹丝不动，那才是「gold 真的很稳」（不可修）。
   这两种结论的后续动作完全相反，只报 gold 名次会把它们混成同一个数字。

③ **清理必须自证恢复**：结束时 `delete_documents_by_prefix(PROBE_PREFIX)`
   + 断言残留 0 + **断言语料指纹回到插入前的值**。
   指纹相同 = 语料逐字恢复（含切片顺序），比"我删了 N 篇"强得多。

--------------------------------------------------------------------------
⚠ 会临时改动共享语料（已知风险，必须告知）
--------------------------------------------------------------------------
插入期间 `corpus_fingerprint_from_db(None)` 会变（它是**全库**指纹），
所以这个窗口里若有别的会话在跑评测，会记录到错误指纹 / `d22_verify` 会红。
→ 本脚本把写入窗口压到最短（插入与清理在**同一次运行**里完成），
  并在 `finally` 中清理 —— 即使中途异常也会撤干净。

用法（项目根目录）：
    uv run python -m scripts.d24_distractor_probe              # 逐篇加到 4 篇
    uv run python -m scripts.d24_distractor_probe --rounds 2   # 只加到 2 篇
    uv run python -m scripts.d24_distractor_probe --force      # 指纹对不上也跑
"""

import argparse
import asyncio
import sys
import time
from collections import Counter

PROJECT_ROOT = "/Users/779369901qq.com/workspace/bs/AgentForge-Lite"
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import app.models  # noqa: F401 —— 导入即注册全部模型
from sqlalchemy import text

from app.core.db import engine
from app.services import document_service, eval_service
from app.services.retrieval_service import (
    DEFAULT_TOP_K,
    RERANK_CANDIDATE_K,
    hybrid_search,
    retrieve,
    search,
    search_keywords,
)
from scripts.d21_cases import CASES
from scripts.d21_corpus import SLUG_TO_FILENAME

# 探测前缀。所有临时文档都带它，清理按同一个前缀收 —— 作用域自限，
# 碰不到 8 篇长期语料（铁律 12 附则二：共享库上的清理必须按前缀自限）。
PROBE_PREFIX = "d24-probe-"

# D23 记录的语料指纹前 8 位。对不上说明库已被改过（别的会话留了残留、
# 或语料被重建），此时"插入前"的基线已经不是 D23 那个语料 —— 拒绝出结论。
EXPECTED_CORPUS_FP_PREFIX = "5783a666"

# 干扰篇要竞争的目标文档：被最多评测题引用的一篇（8 条：A05~A08 + B01/B02/B05/B09）。
TARGET_SLUG = "travel-expense"


# ============================================================
# 干扰篇正文（合成内容，只用于本次模拟）
# ============================================================
# 设计规则（三条，都能机检）：
#   ① **同主题词**：必须出现「差旅费用 / 住宿费标准 / 伙食补助 / 报销申请 /
#      交通工具 / 出差申请」等同一条款主题词，否则"同主题"只是我自己说的；
#   ② **不同结论**：住宿限额、补助标准、报销时限、审批金额全部与 gold 不同 ——
#      这样干扰篇一旦被检索到，答案就会变（这正是它要制造的失败模式）；
#   ③ **体量与形态接近**：13~14 条条款、1400~1700 字，与目标文档同量级。
#      否则切片数差异会让"挤进前 5 的能力"不可比。
TWINS: list[tuple[str, list[str]]] = [
    (
        "子公司及境外机构差旅费用管理实施细则.md",
        [
            "第一条　目的与适用范围。为规范全资及控股子公司、境外机构的差旅费用管理，结合子公司业务特点，制定本实施细则。本细则适用于子公司全体员工的公务出差活动，境外机构驻外人员因公往返的差旅费用参照执行。",
            "第二条　出差的申请与审批。子公司员工出差应当事先提交出差申请单，注明出差事由、目的地、起止日期及预算金额。出差 5 天以内的，由子公司部门负责人审批；出差超过 5 天的，由子公司总经理审批，并报母公司分管部门备案。",
            "第三条　交通工具的选择标准。城际出行距离在 200 公里以内的，优先选择高铁二等座；距离超过 200 公里的，可以乘坐飞机经济舱。子公司部门负责人及以上人员可乘坐高铁一等座。境外差旅的舱位标准按照母公司因公出境管理规定执行。",
            "第四条　住宿费标准。住宿费实行限额管理，凭据报销。直辖市、省会城市及计划单列市的住宿费标准为每人每晚不超过 600 元；其他地级市为每人每晚不超过 450 元；县级市及以下地区为每人每晚不超过 350 元。境外机构所在地的住宿费标准另行制定。",
            "第五条　住宿费超标处理。因会议、展会等客观原因导致住宿费超出标准的，应当在报销时提供会议通知或者酒店价格证明，经子公司总经理确认后据实报销。无正当理由超出住宿标准的，超出部分由个人承担。",
            "第六条　伙食补助标准。出差期间发放伙食补助费，标准为每人每天 120 元，按实际出差天数计算。当日往返的出差，伙食补助按每天 60 元计算。已由对方单位或者会议主办方提供餐饮的，相应餐次不再计发伙食补助。",
            "第七条　市内交通费。出差期间发生的市内交通费，凭据据实报销，包括机场大巴、地铁、公交、出租车及网约车费用。单次市内交通费超过 300 元的，应当说明原因。",
            "第八条　报销时限与凭证要求。员工应当在出差结束后 20 个工作日内提交报销申请。报销凭证应当包括出差申请单、交通票据、住宿发票及消费明细。发票抬头必须为子公司全称，发票内容应当与实际消费一致。",
            "第九条　报销审批流程。报销申请由子公司部门负责人初审，子公司财务部门复核票据合规性与预算执行情况，子公司总经理审批。单笔报销金额超过 30000 元的，需报母公司财务负责人审批。财务部门应当在收到完整报销单据后 7 个工作日内完成复核。",
            "第十条　特殊情形的差旅费用。子公司员工参加外部培训、外部会议的，其差旅费用按照本细则执行。因紧急公务需要临时改变行程的，应当及时向审批人报备，并在报销时说明变更原因。",
            "第十一条　境外差旅。子公司员工因公出境的，应当事先报子公司总经理批准，并按国家有关规定办理审批手续。境外差旅的交通、住宿标准参照国家有关因公临时出国经费管理规定执行，报销时应当附汇率折算说明。",
            "第十二条　预借差旅费。预计差旅费用超过 8000 元的，员工可以申请预借差旅费。预借款应当在出差结束后 20 个工作日内办理核销手续。前次预借款未核销的，不得再次申请预借。预借金额不得超过预计差旅费用的 70%。",
            "第十三条　违规处理。虚报、冒领差旅费用的，除追回款项外，视情节轻重给予相应处理。子公司负责人对本单位差旅费用的真实性承担审核责任。",
            "第十四条　附则。本细则由子公司财务部门负责解释，自发布之日起施行。本细则与母公司差旅费用管理办法不一致的，以本细则为准。",
        ],
    ),
    (
        "差旅费用报销标准与管理办法（2019修订版·已废止）.md",
        [
            "第一条　目的与适用范围。为规范公司差旅费用的申请、审批与报销流程，合理控制差旅成本，根据公司财务管理制度有关规定，制定本办法。本办法适用于公司全体员工的公务出差活动。",
            "第二条　出差的申请与审批。员工出差应当事先提交出差申请单，注明出差事由、目的地、起止日期、随行人员及预算金额。出差 3 天以内的，由部门经理审批；出差超过 3 天的，由部门经理审核后报分管副总经理审批。",
            "第三条　交通工具的选择标准。城际出行距离在 250 公里以内的，优先选择高铁二等座；距离超过 250 公里的，可以乘坐飞机经济舱。部门经理及以上人员可乘坐高铁一等座。",
            "第四条　住宿费标准。住宿费实行限额管理，凭据报销。直辖市、省会城市及计划单列市等一线城市的住宿费标准为每人每晚不超过 450 元；其他地级市为每人每晚不超过 350 元；县级市及以下地区为每人每晚不超过 250 元。",
            "第五条　住宿费超标处理。因会议、展会等客观原因导致住宿费超出标准的，应当在报销时提供会议通知或者酒店价格证明，经部门经理确认后据实报销。",
            "第六条　伙食补助标准。出差期间发放伙食补助费，标准为每人每天 80 元，按实际出差天数计算。当日往返的出差，伙食补助按每天 40 元计算。",
            "第七条　市内交通费。出差期间发生的市内交通费，凭据据实报销，包括机场大巴、地铁、公交、出租车及网约车费用。单次市内交通费超过 150 元的，应当说明原因。",
            "第八条　报销时限与凭证要求。员工应当在出差结束后 30 个工作日内提交报销申请。报销凭证应当包括出差申请单、交通票据、住宿发票及消费明细。",
            "第九条　报销审批流程。报销申请由部门经理初审，财务部门复核票据合规性，分管副总经理审批。单笔报销金额超过 15000 元的，需报总经理审批。财务部门应当在收到完整报销单据后 3 个工作日内完成复核。",
            "第十条　特殊情形的差旅费用。员工参加外部培训、外部会议的，其差旅费用按照本办法执行。",
            "第十一条　预借差旅费。预计差旅费用超过 4000 元的，员工可以申请预借差旅费。预借款应当在出差结束后 30 个工作日内办理核销手续。预借金额不得超过预计差旅费用的 90%。",
            "第十二条　违规处理。虚报、冒领差旅费用的，除追回款项外，视情节轻重给予相应处理。部门经理对本部门差旅费用的真实性承担审核责任。",
            "第十三条　附则。本办法由财务部门负责解释。本办法自 2019 年 1 月 1 日起施行，2023 年 12 月 31 日起废止，相关事项按现行《差旅费用报销标准与管理办法》执行。",
        ],
    ),
    (
        "华东区域中心差旅与商务接待费用补充规定.md",
        [
            "第一条　目的与适用范围。为统一华东区域中心各办事处的差旅与商务接待费用管理，制定本补充规定。本规定适用于华东区域中心及各办事处员工在区域内的公务出差与商务接待活动。",
            "第二条　区域内的出差审批。员工在区域中心所在城市以外出差，应当事先提交出差申请单。出差 2 天以内的，由办事处负责人审批；出差超过 2 天的，由区域中心负责人审批。",
            "第三条　交通工具的选择标准。区域中心与各办事处之间的往返，优先选择高铁二等座；距离超过 350 公里的，可以乘坐飞机经济舱。区域内城市间的短途出行，优先使用高铁或者城际大巴。",
            "第四条　住宿费标准。住宿费实行限额管理，凭据报销。直辖市、省会城市及计划单列市的住宿费标准为每人每晚不超过 550 元；其他地级市为每人每晚不超过 420 元；县级市及以下地区为每人每晚不超过 320 元。",
            "第五条　伙食补助标准。出差期间发放伙食补助费，标准为每人每天 110 元，按实际出差天数计算。当日往返的出差，伙食补助按每天 55 元计算。",
            "第六条　商务接待费用。因业务需要接待客户的，应当事先提交接待申请，注明接待对象、人数、事由及预算。人均接待标准不超过 200 元。接待费用不得与差旅费用混合报销。",
            "第七条　报销时限与凭证要求。员工应当在出差结束后 10 个工作日内提交报销申请。报销凭证应当包括出差申请单、交通票据、住宿发票及消费明细。发票抬头必须为公司全称。",
            "第八条　报销审批流程。报销申请由办事处负责人初审，区域中心财务岗复核，区域中心负责人审批。单笔报销金额超过 10000 元的，需报公司财务部门复核。",
            "第九条　特殊情形的差旅费用。区域中心员工参加外部培训、外部会议的，其差旅费用按照本补充规定执行。因紧急公务需要临时改变行程的，应当及时向审批人报备。",
            "第十条　市内交通费。出差期间发生的市内交通费，凭据据实报销，包括机场大巴、地铁、公交、出租车及网约车费用。单次市内交通费超过 200 元的，应当说明原因。",
            "第十一条　违规处理。虚报、冒领差旅费用，或者将商务接待费用混入差旅费用报销的，除追回款项外，视情节轻重给予相应处理。",
            "第十二条　附则。本补充规定由区域中心负责解释，自发布之日起施行。本补充规定与公司差旅费用管理办法不一致的，以本补充规定为准。",
        ],
    ),
    (
        "项目现场驻场人员差旅费用管理特别规定.md",
        [
            "第一条　目的与适用范围。为规范项目现场驻场人员的差旅费用管理，保障驻场工作的正常开展，制定本特别规定。本规定适用于派驻项目现场连续工作 1 个月以上的员工。",
            "第二条　驻场期间的生活补助。驻场人员发放生活补助费，标准为每人每天 130 元，按实际驻场天数计算。项目现场提供住宿及餐饮的，生活补助减半发放。",
            "第三条　住宿费标准。驻场期间由项目统一安排住宿的，不再报销住宿费。自行安排住宿的，直辖市、省会城市及计划单列市的住宿费标准为每人每晚不超过 700 元；其他地级市为每人每晚不超过 500 元。",
            "第四条　往返项目现场的交通。驻场人员往返项目现场，优先选择高铁二等座；距离超过 200 公里的，可以乘坐飞机经济舱。每 3 个月可以回基地探亲一次，探亲往返交通费用按本规定报销。",
            "第五条　报销时限与凭证要求。驻场人员应当在每个驻场周期结束后 25 个工作日内提交报销申请。报销凭证应当包括出差申请单、交通票据、住宿发票及消费明细。",
            "第六条　报销审批流程。报销申请由项目经理初审，项目管理部门复核，分管副总经理审批。单笔报销金额超过 20000 元的，需报总经理审批。",
            "第七条　伙食补助与市内交通费。驻场期间的伙食补助不再重复发放；因公往返项目现场以外的公务活动发生的伙食补助，按每人每天 100 元执行。",
            "第八条　预借差旅费。预计差旅费用超过 6000 元的，员工可以申请预借差旅费。预借款应当在驻场周期结束后 25 个工作日内办理核销手续。预借金额不得超过预计差旅费用的 80%。",
            "第九条　出差申请与报备。驻场人员离开项目现场办理其他公务的，应当事先向项目经理报备，并注明事由与返回时间。未经报备的费用不予报销。",
            "第十条　违规处理。虚报、冒领驻场补助及差旅费用的，除追回款项外，视情节轻重给予相应处理。项目经理对驻场费用的真实性承担审核责任。",
            "第十一条　附则。本特别规定由项目管理部门负责解释，自发布之日起施行。本规定与公司差旅费用管理办法不一致的，以本特别规定为准。",
        ],
    ),
]

# 探测窗口。必须 ≥ 重排窗口，否则"名次 20 以外"会与"没命中"混成同一个 None。
DEFAULT_PROBE_K = RERANK_CANDIDATE_K

# 四条链路（与 d24_rank_probe 同一口径）
LINKS = (
    ("vec", lambda q, k: search(q, top_k=k)),
    ("bm25", lambda q, k: search_keywords(q, top_k=k)),
    ("fused", lambda q, k: hybrid_search(q, top_k=k)),
    ("rerank", lambda q, k: retrieve(q, top_k=k, config="hybrid_rerank")),
)


# ============================================================
# 数据准备
# ============================================================
def computable_cases() -> list[dict]:
    """
    只取「有 gold 文档」的题 —— 5 条负例题没有依据文档，不参与名次统计。

    判据用**数据**（doc_slugs 为空）而不是类别名：类别名是出题人的标签，
    将来加了新类别就会被漏掉（D23 的教训）。
    """
    return [c for c in CASES if c["doc_slugs"]]


async def slug_to_uuid(conn) -> dict[str, object]:
    """
    doc_slug → document UUID。

    `documents` 表**没有** slug 列（实测 information_schema：只有 id / filename /
    file_type / status / chunk_count / error_message / created_at），
    slug 是出题侧的业务标识，靠 SLUG_TO_FILENAME 反查文件名再对 UUID。
    """
    rows = (await conn.execute(text("select id, filename from documents"))).mappings().all()
    by_filename = {r["filename"]: r["id"] for r in rows}
    return {
        slug: by_filename[fn]
        for slug, fn in SLUG_TO_FILENAME.items()
        if fn in by_filename
    }


async def probe_uuid(conn) -> dict[str, object]:
    """本次插入的干扰篇：filename → UUID（按 PROBE_PREFIX 现查，不靠返回值）。"""
    rows = (
        await conn.execute(
            text("select id, filename from documents where filename like :p escape '\\'"),
            {"p": PROBE_PREFIX + "%"},
        )
    ).mappings().all()
    return {r["filename"]: r["id"] for r in rows}


async def chunks_to_docs(conn, hits: list[dict]) -> dict[str, object]:
    """切片 id → 所属文档 UUID（检索结果里只有 chunk_id，没有文档身份）。"""
    ids = [h["chunk_id"] for h in hits if h.get("chunk_id") is not None]
    if not ids:
        return {}
    rows = (
        await conn.execute(
            text("select id, document_id from document_chunks where id = any(:ids)"),
            {"ids": [int(x) for x in ids]},
        )
    ).mappings().all()
    return {str(r["id"]): r["document_id"] for r in rows}


def ranks_for(hits: list[dict], chunk2doc: dict, wanted: set) -> dict:
    """
    给定一批检索结果，返回 `{文档 UUID: 最早名次}`（只含 wanted 里的文档）。

    文档级 + 取最早名次：一篇文档有很多片，切片级名次会被"同篇命中好几片"稀释虚高，
    而这个名次要拿去和 `DEFAULT_TOP_K`（条数）比 —— 两个口径必须一致。
    """
    best: dict = {}
    for rank, h in enumerate(hits, start=1):
        did = chunk2doc.get(str(h.get("chunk_id")))
        if did is not None and did in wanted:
            best.setdefault(did, rank)
    return best


async def measure(conn, question: str, watch: set, k: int) -> dict:
    """
    一条题 × 四条链路 → 每条链路上 watch 集合里每个文档的名次。

    四条链路各自独立跑一遍、**不复用**：复用会让"某条链路其实没跑到"看不出来，
    而本探针的全部价值就在于区分"哪条链路把它排到哪"。
    """
    out: dict = {}
    for label, fn in LINKS:
        try:
            hits = await fn(question, k)
        except Exception as e:  # noqa: BLE001 —— 单条链路失败不该中断整轮
            out[label] = None
            out[f"{label}_err"] = f"{type(e).__name__}: {e}"
            continue
        c2d = await chunks_to_docs(conn, hits)
        out[label] = ranks_for(hits, c2d, watch)
    return out


def worst(ranks: dict | None, gold: set, k: int) -> int:
    """
    最差的那篇 gold 的名次（未命中记 k+1，即「窗口外」）。

    为什么报「最差」而不只报「最好」：跨文档题要**两篇都拿到**才算答得全。
    只报最好那篇会把「另一篇已经掉出窗口」这件事藏起来。

    `ranks is None` 表示这条链路**整条报错**了（见 `measure`）——
    此时按「窗口外」归档而不是崩掉：一条链路挂掉不该让整轮没有结论，
    但它会在①自检段被单独列出来（所以不会被当成正常结果）。
    """
    if ranks is None:
        return k + 1
    return max([ranks.get(d, k + 1) for d in gold])


def best_of(ranks: dict | None, gold: set, k: int) -> int:
    """第一篇 gold 的名次（未命中记 k+1）—— 与 `worst` 成对，展示用。"""
    if ranks is None:
        return k + 1
    return min([ranks.get(d, k + 1) for d in gold])


def zone(rank: int, k: int) -> str:
    """名次落在哪个区间 —— 用词由数据算出来，不硬编码（铁律 11）。"""
    if rank <= DEFAULT_TOP_K:
        return f"1~{DEFAULT_TOP_K}"
    if rank <= k:
        return f"{DEFAULT_TOP_K + 1}~{k}"
    return f">{k}"


# ============================================================
# 主流程
# ============================================================
async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=len(TWINS),
                    help=f"逐篇加到第几篇（默认 {len(TWINS)}，上限 {len(TWINS)}）")
    ap.add_argument("--top-k", type=int, default=DEFAULT_PROBE_K,
                    help=f"探测窗口（默认 {DEFAULT_PROBE_K}，必须 ≥ {RERANK_CANDIDATE_K}）")
    ap.add_argument("--force", action="store_true",
                    help="语料指纹与 D23 记录不符时也继续（默认拒绝出结论）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只量基线、**不插入任何文档**（先验证链路与指纹，再决定要不要进写入窗口）")
    args = ap.parse_args()

    k = args.top_k
    if k < RERANK_CANDIDATE_K:
        print(f"--top-k 必须 ≥ {RERANK_CANDIDATE_K}（重排窗口），收到 {k} —— 拒绝出结果。")
        return
    rounds = max(1, min(args.rounds, len(TWINS)))

    cases = computable_cases()
    target_cases = [c for c in cases if TARGET_SLUG in c["doc_slugs"]]

    # ---- 插入前状态快照：清理时要靠它自证恢复 ----
    fp_before, chunks_before = await eval_service.corpus_fingerprint_from_db(None)
    docs_before = await document_service.count_documents()

    print("=" * 100)
    print("插入前状态快照")
    print("=" * 100)
    print(f"  文档 {docs_before} 篇 / 切片 {chunks_before} 片")
    print(f"  语料指纹 {fp_before[:16]}…")
    stale = await document_service.count_documents_by_prefix(PROBE_PREFIX)
    print(f"  前缀 {PROBE_PREFIX!r} 残留：{stale} 篇   ← 应为 0")

    if not fp_before.startswith(EXPECTED_CORPUS_FP_PREFIX) and not args.force:
        print()
        print(f"⚠ 语料指纹与 D23 记录（{EXPECTED_CORPUS_FP_PREFIX}…）不符 —— **拒绝出结论**。")
        print("  原因：插入前的基线已经不是 D23 那个语料，名次变化无法归因到干扰篇。")
        print("  先查清库里多了/少了什么（`count_documents_by_prefix`），或加 --force 强制继续。")
        await engine.dispose()
        return

    async with engine.connect() as conn:
        s2u = await slug_to_uuid(conn)
        missing = sorted({s for c in cases for s in c["doc_slugs"]} - set(s2u))
        if missing:
            print(f"⚠ 语料里找不到这些 slug 对应的文档：{missing}")
            print("  先跑 scripts/d21_seed.py 重建语料 —— 本探针不下任何结论。")
            await engine.dispose()
            return

        print()
        print(f"可算题 {len(cases)} 条；其中 gold 含 {TARGET_SLUG} 的 {len(target_cases)} 条"
              f"（{', '.join(c['case_key'] for c in target_cases)}）")
        print(f"探测窗口 top_k = {k}（B 配置取前 {DEFAULT_TOP_K} 条；C 配置重排窗口 {RERANK_CANDIDATE_K}）")

        # 基线（未插入任何干扰篇）
        print("\n跑基线（未插入）…", flush=True)
        rounds_data: list[dict] = [await measure_all(conn, cases, s2u, k, label="基线")]

        if args.dry_run:
            # 写入窗口是本次唯一有风险的动作 —— 先让调用方能只跑基线验证链路与指纹，
            # 确认无误再进窗口（而不是"先插进去看看"）。
            print("\n--dry-run：跳过插入与清理（库里一个字节都没动）", flush=True)
        else:
            try:
                for i in range(rounds):
                    name, clauses = TWINS[i]
                    filename = PROBE_PREFIX + name
                    print(f"\n插入第 {i + 1} 篇：{filename}", flush=True)
                    _doc_id, n_chunks = await document_service.ingest_texts(
                        ["\n".join(clauses)], filename=filename
                    )
                    print(f"  入库成功 → {n_chunks} 片；语料现在 "
                          f"{await document_service.count_documents()} 篇 / "
                          f"{await document_service.count_chunks()} 片", flush=True)

                    p2u = await probe_uuid(conn)
                    watch = {s2u[s] for c in cases for s in c["doc_slugs"]} | set(p2u.values())
                    rounds_data.append(
                        await measure_all(conn, cases, s2u, k, label=f"+{i + 1} 篇", watch=watch,
                                          probe_ids=set(p2u.values()))
                    )
            finally:
                # ---- 清理：即使中途异常也必须撤干净 ----
                print("\n清理临时干扰篇…", flush=True)
                removed = await document_service.delete_documents_by_prefix(PROBE_PREFIX)
                print(f"  delete_documents_by_prefix({PROBE_PREFIX!r}) → 删除 {removed} 篇")

    # ---- 自证恢复（三条，全部可失败）----
    residue = await document_service.count_documents_by_prefix(PROBE_PREFIX)
    docs_after = await document_service.count_documents()
    fp_after, chunks_after = await eval_service.corpus_fingerprint_from_db(None)

    print()
    print("=" * 100)
    print("① 自证恢复")
    print("=" * 100)
    print(f"  前缀残留                  ：{residue} 篇   ← 必须为 0")
    print(f"  文档数 {docs_before} → {docs_after}   ← 必须相等")
    print(f"  切片数 {chunks_before} → {chunks_after}   ← 必须相等")
    print(f"  语料指纹 {fp_before[:16]}… → {fp_after[:16]}…   ← 必须相同")
    restored = (
        residue == 0
        and docs_after == docs_before
        and chunks_after == chunks_before
        and fp_after == fp_before
    )
    print(f"  → 语料已逐字恢复：{restored}")
    if not restored:
        print("  ⚠ 恢复失败 —— 后续所有结论作废，先手工清库。")

    # ---- 剂量-反应曲线 ----
    print()
    print("=" * 100)
    print("② 剂量-反应：每多一篇干扰篇，目标题的 gold 名次怎么动")
    print("=" * 100)
    print(f"{'轮次':<8}{'题号':<6}{'向量':>6}{'词法':>6}{'融合':>6}{'重排':>6}   {'融合区间':<10}{'干扰篇最好名次':>14}")
    for rd in rounds_data:
        for c in target_cases:
            m = rd["per_case"][c["case_key"]]
            gold = {s2u[s] for s in c["doc_slugs"]}
            w = worst(m["fused"], gold, k)
            # 本次插入的干扰篇在这一轮里的**最好名次**（没挤进窗口就是 None）
            probe_ranks = [r for d, r in (m["fused"] or {}).items() if d in rd["probe_ids"]]
            probe_best = min(probe_ranks) if probe_ranks else None
            print(
                f"{rd['label']:<8}{c['case_key']:<6}"
                f"{best_of(m['vec'], gold, k):>6}"
                f"{best_of(m['bm25'], gold, k):>6}"
                f"{best_of(m['fused'], gold, k):>6}"
                f"{best_of(m['rerank'], gold, k):>6}   "
                f"{zone(w, k):<10}{str(probe_best):>14}"
            )
        print("-" * 100)

    # ---- 分布迁移（全部可算题）----
    print()
    print("=" * 100)
    print("③ 分布迁移：全 35 条题的 gold 落在哪个区间（按链路看）")
    print("=" * 100)
    for link in ("vec", "fused", "rerank"):
        print(f"\n[{link}]")
        print(f"{'轮次':<8}" + "".join(f"{zo:<12}" for zo in
              (f"1~{DEFAULT_TOP_K}", f"{DEFAULT_TOP_K + 1}~{k}", f">{k}")))
        for rd in rounds_data:
            cnt = Counter()
            for c in cases:
                gold = {s2u[s] for s in c["doc_slugs"]}
                cnt[zone(worst(rd["per_case"][c["case_key"]][link], gold, k), k)] += 1
            print(f"{rd['label']:<8}" + "".join(
                f"{cnt.get(zo, 0):<12}" for zo in
                (f"1~{DEFAULT_TOP_K}", f"{DEFAULT_TOP_K + 1}~{k}", f">{k}")))
    print()
    print("（分母都是全 35 条可算题；跨文档题按**最差那篇 gold** 归档，")
    print("  即「缺一篇就算掉出该区间」——这是更严的口径。）")

    # ---- 目标题的迁移（乙案真正关心的那 8 条）----
    print()
    print("=" * 100)
    print("④ 目标题迁移：必须由数据决定用词")
    print("=" * 100)
    base = rounds_data[0]
    last = rounds_data[-1]
    moved = 0
    for c in target_cases:
        gold = {s2u[s] for s in c["doc_slugs"]}
        b = worst(base["per_case"][c["case_key"]]["fused"], gold, k)
        a = worst(last["per_case"][c["case_key"]]["fused"], gold, k)
        if zone(b, k) != zone(a, k):
            moved += 1
    push = []
    for c in target_cases:
        gold = {s2u[s] for s in c["doc_slugs"]}
        b = worst(base["per_case"][c["case_key"]]["fused"], gold, k)
        a = worst(last["per_case"][c["case_key"]]["fused"], gold, k)
        push.append(a - b)
    probe_hits = sum(
        1 for c in target_cases
        if any(
            r <= DEFAULT_TOP_K
            for d, r in last["per_case"][c["case_key"]]["fused"].items()
            if d in last["probe_ids"]
        )
    )
    print(f"  目标题 {len(target_cases)} 条，加了 {rounds} 篇干扰篇后：")
    print(f"  · 融合名次被推动的题数（区间发生变化）：{moved}/{len(target_cases)}")
    print(f"  · 名次平均被推后：{sum(push) / len(push):.1f} 位（最小 {min(push)}，最大 {max(push)}）")
    print(f"  · 干扰篇**自己挤进融合 top{DEFAULT_TOP_K}** 的题数：{probe_hits}/{len(target_cases)}")
    print()

    # ============================================================
    # ⑤ 自检 —— 防「某条链路悄悄挂了，被当成窗口外」
    # ============================================================
    print("=" * 100)
    print("⑤ 自检")
    print("=" * 100)
    errs = sorted({
        (rd["label"], ck, key)
        for rd in rounds_data
        for ck, m in rd["per_case"].items()
        for key, v in m.items()
        if key.endswith("_err") and v
    })
    print(f"链路报错的（轮次, 题号, 链路）：{errs or '无'}   ← 应为空")
    # 「链路挂了」和「名次在窗口外」在数字上长得一样，必须单独断出来
    assert not errs, f"有链路报错，名次不可信：{errs}"
    print(f"首轮是基线（未插入任何干扰篇）吗？{rounds_data[0]['label'] == '基线'}   ← 应为 True")
    expected_rounds = 1 if args.dry_run else rounds + 1
    print(f"轮次数 = 1 + 实际插入篇数？{len(rounds_data) == expected_rounds}   ← 应为 True"
          f"（本轮预期 {expected_rounds}）")
    print(f"每轮都覆盖了全部 {len(cases)} 条题吗？"
          f"{all(len(rd['per_case']) == len(cases) for rd in rounds_data)}   ← 应为 True")
    expected_probe = 0 if args.dry_run else rounds
    print(f"最后一轮识别到的干扰篇数：{len(last['probe_ids'])}   ← 应为 {expected_probe}")
    print(f"基线轮识别到的干扰篇数：{len(base['probe_ids'])}   ← 应为 0")
    print(f"每轮耗时（秒）：{[(rd['label'], round(rd['seconds'], 1)) for rd in rounds_data]}")
    print("  （逐轮耗时是判断「这一轮是不是被机器负载拖慢」的唯一线索 —— "
          "D18 同一份代码两次差 1.8 倍，就是靠它发现的）")
    assert len(rounds_data) == expected_rounds
    assert all(len(rd["per_case"]) == len(cases) for rd in rounds_data)
    assert len(last["probe_ids"]) == expected_probe
    assert len(base["probe_ids"]) == 0

    print()
    # ============================================================
    # ⑥ 关键读数 —— 全部由数据算出（禁止手抄上表里的数字）
    # ============================================================
    # 为什么单独一段：上面的表是"原始数据"，结论要用的位移量、单调性、临界名单
    # 必须由**代码算**出来。手抄数字进结论 = 铁律 11 的老坑（数据反了、文字照念）。
    print("=" * 100)
    print("⑥ 关键读数")
    print("=" * 100)
    last_probe = last["probe_ids"]
    rows_read: list[tuple] = []
    for c in target_cases:
        gold = {s2u[s] for s in c["doc_slugs"]}
        vecs = [best_of(rd["per_case"][c["case_key"]]["vec"], gold, k) for rd in rounds_data]
        fuses = [best_of(rd["per_case"][c["case_key"]]["fused"], gold, k) for rd in rounds_data]
        rows_read.append((c["case_key"], vecs, fuses))

    print(f"{'题号':<6}{'向量名次（基线 → 逐篇）':<34}{'向量位移':>8}{'单调不减':>10}"
          f"{'融合名次（基线 → 逐篇）':<34}{'融合位移':>8}")
    for key, vecs, fuses in rows_read:
        mono = all(b >= a for a, b in zip(vecs, vecs[1:]))
        print(f"{key:<6}{str(vecs):<34}{vecs[-1] - vecs[0]:>8}{str(mono):>10}"
              f"{str(fuses):<34}{fuses[-1] - fuses[0]:>8}")

    vec_deltas = [v[-1] - v[0] for _, v, _ in rows_read]
    fuse_deltas = [f[-1] - f[0] for _, _, f in rows_read]
    mono_vec = sum(1 for _, v, _ in rows_read if all(b >= a for a, b in zip(v, v[1:])))
    print()
    print(f"  向量路位移：最大 {max(vec_deltas)}，平均 {sum(vec_deltas) / len(vec_deltas):.2f}"
          f"（{sum(1 for d in vec_deltas if d > 0)}/{len(vec_deltas)} 条被推后）")
    print(f"  融合路位移：最大 {max(fuse_deltas)}，平均 {sum(fuse_deltas) / len(fuse_deltas):.2f}"
          f"（{sum(1 for d in fuse_deltas if d > 0)}/{len(fuse_deltas)} 条被推后）")
    print(f"  向量名次严格单调不减的题：{mono_vec}/{len(rows_read)}"
          "   ← 单调 = 干扰篇的推力是「可累加」的，才能按斜率外推")
    print(f"  跨路口径：每篇干扰篇在向量路的平均推力 "
          f"= {sum(vec_deltas) / len(vec_deltas) / rounds:.2f} 位/篇；"
          f"在融合路 = {sum(fuse_deltas) / len(fuse_deltas) / rounds:.2f} 位/篇")

    # ---- 临界名单：向量名次已经贴到 A 配置（取 top_k 条）的边界 ----
    print()
    print(f"  临界名单（向量名次 ≥ {DEFAULT_TOP_K - 1}，即「再 1 篇就掉出 A 的 top{DEFAULT_TOP_K}」）：")
    brink = [
        (key, v[-1], f[-1]) for key, v, f in rows_read if v[-1] >= DEFAULT_TOP_K - 1
    ]
    if brink:
        for key, vr, fr in brink:
            print(f"    {key}：向量第 {vr} 名、融合第 {fr} 名"
                  f" → 再推 {(DEFAULT_TOP_K + 1) - vr} 位即满足「A 拿不到、B 拿得到」")
    else:
        print("    无 —— 向量路也还没推到边界")

    # ---- 干扰篇自己挤进来的程度（决定"结论归因给谁"）----
    print()
    print("  干扰篇自己的融合名次（每轮取最好的一篇；None = 全部没进窗口）：")
    for rd in rounds_data[1:]:
        bests = []
        for c in target_cases:
            m = rd["per_case"][c["case_key"]]["fused"] or {}
            r = [v for d, v in m.items() if d in last_probe]
            bests.append(min(r) if r else None)
        hit5 = sum(1 for b in bests if b is not None and b <= DEFAULT_TOP_K)
        print(f"    {rd['label']:<8}{bests}   进 top{DEFAULT_TOP_K}：{hit5}/{len(target_cases)}")

    # ---- 全局副作用：加语料是全局操作，方向不定 ----
    print()
    print("  全局副作用（全 35 条题，融合路区间计数 基线 → 最后一轮）：")
    for zo in (f"1~{DEFAULT_TOP_K}", f"{DEFAULT_TOP_K + 1}~{k}", f">{k}"):
        b = sum(1 for c in cases
                if zone(worst(base["per_case"][c["case_key"]]["fused"],
                              {s2u[s] for s in c["doc_slugs"]}, k), k) == zo)
        a = sum(1 for c in cases
                if zone(worst(last["per_case"][c["case_key"]]["fused"],
                              {s2u[s] for s in c["doc_slugs"]}, k), k) == zo)
        print(f"    {zo:<12}{b} → {a}   （{a - b:+d}）")
    print()
    print("  ⚠ 上表两个方向的箭头同时出现是**预期内的**：加文档会改变 BM25 的")
    print("    N / avgdl（全库量），所有查询的打分都会漂 —— 干扰不是「只让目标题变难」。")

    print()
    # —— 结论：分支由数据决定，且**分链路**说 ——
    # 为什么必须分链路：干扰篇对「向量路」与「融合/重排路」的推力差一个量级
    # （见上表）。合成一句话会把最有用的信息——**哪条路能被撼动**——丢掉。
    if args.dry_run:
        # 没插入任何东西就没有"剂量"，此时任何结论都是无源之水 ——
        # 显式说"不下结论"，而不是让它掉进下面某个分支（那就是给没测的分支下结论）
        print("→ 本次是 --dry-run（未插入任何文档）—— **不下任何结论**。")
        print("  已完成的验证：链路跑通、题目可算、指纹与 D23 记录一致。")
    elif probe_hits == 0:
        print("→ 结论：**干扰篇根本没挤进 top5** —— 问题不在 gold 太强，在干扰篇不够像。")
        print("  这种情况下「要写几篇」无从谈起：先把干扰篇写到能与 gold 同台（复现同一主题词与句式），再重测。")
    else:
        print(f"→ 一、干扰篇确实挤进来了：{probe_hits}/{len(target_cases)} 条目标题上它进了融合 "
              f"top{DEFAULT_TOP_K}（有的直接排第 1 名）—— 「同主题竞争」本身是造得出来的。")
        print("     所以下面这条推力不足的结论，不能归因于「干扰篇不够像」。")
        print()
        print("→ 二、但它对两条链路的推力**不是一个量级**：")
        print(f"     · 向量路：平均 {sum(vec_deltas) / len(vec_deltas):.2f} 位"
              f"（最大 {max(vec_deltas)}），{mono_vec}/{len(rows_read)} 条**单调不减**")
        print(f"     · 融合路：平均 {sum(fuse_deltas) / len(fuse_deltas):.2f} 位"
              f"（最大 {max(fuse_deltas)}）")
        if brink:
            print(f"   → **{len(brink)} 条已贴到 A 配置的边界**（向量第 {DEFAULT_TOP_K} 名）："
                  "再写 1 篇同类干扰篇，就可能造出「A 拿不到、B 拿得到」。")
            print("     那正是分开 A 与 B 的条件，也正是 PRD S2 验收点"
                  "「混合+重排 ≥ 纯向量」要考的东西。")
        else:
            print("   → 向量路离边界也还远：需要更多篇，或换更强的干扰形态。")
        print(f"   → 融合路推力**饱和**（平均 {sum(fuse_deltas) / len(fuse_deltas):.2f} 位，"
              "各题停在 2~3 名不动）：")
        print("     干扰篇只能抢走融合序的第 1 名，其余同名干扰篇都排在 gold **之后** ——")
        print(f"     所以「把 gold 压进 {DEFAULT_TOP_K + 1}~{k} 区间（唯一能分开 B/C 的区间）"
              "」这条路做不到。")
        print()
        print("→ 三、方向修正：乙案若做，产出的是 **A vs B/C 的区分**，不是 B vs C 的区分。")
        print("     想让融合路也动，干扰篇必须同时在 **BM25 路**上压过 gold ——")
        print("     也就是要「逐字近乎重复」而不只是「同主题、不同结论」。")
    print()
    print("⚠ 本次插入的是**合成干扰篇**，只为测「竞争能不能造出来」。")
    print("  它不代表真实企业文档的分布；要写进报告必须先声明这一点。")

    await engine.dispose()

    # 清理没做干净 = 硬失败（退出码非 0）。放在最末尾是有意的：
    # 先让上面的报告完整打出来（否则你不知道少了什么就已被中断），
    # 再用退出码把「这批结论作废」变成一个不可忽略的事实 ——
    # 只在屏幕中间印一行「⚠ 恢复失败」等于没发生（铁律 12：静默失效）。
    assert restored, "语料未逐字恢复（残留 / 文档数 / 切片数 / 指纹 有对不上的）—— 后续结论作废。"


async def measure_all(conn, cases, s2u, k, label, watch=None, probe_ids=None) -> dict:
    """一轮 = 全部可算题 × 四条链路。返回 {label, per_case, probe_ids, seconds}。"""
    if watch is None:
        watch = {s2u[s] for c in cases for s in c["doc_slugs"]}
    if probe_ids is None:
        probe_ids = set()
    per_case: dict = {}
    started = time.perf_counter()   # 计时起点落在"这一轮真实的检索工作量"上
    for i, c in enumerate(cases, 1):
        gold = {s2u[s] for s in c["doc_slugs"]}
        per_case[c["case_key"]] = await measure(conn, c["question"], watch | gold, k)
        if i % 10 == 0:
            print(f"    {label}: {i}/{len(cases)}", flush=True)
    elapsed = time.perf_counter() - started
    print(f"  {label} 完成（{len(cases)} 条，耗时 {elapsed:.1f}s）", flush=True)
    return {"label": label, "per_case": per_case, "probe_ids": probe_ids, "seconds": elapsed}


if __name__ == "__main__":
    asyncio.run(main())
