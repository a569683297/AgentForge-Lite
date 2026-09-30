"""
D24 挑战题（D 类）：同一批答案，换成「用户自己会说的话」来问
===============================================================

这份文件要回答的问题
--------------------
D23 第④步测出：三配置（pure_vector / hybrid / hybrid_rerank）在检索层
**Hit@1/5/10 全 100%、MRR 恒 1.000、35/35 题 gold 都排第 1 名**。
D24 开工前的三个零 token 探针把原因定位到了精确的位置：

  用**最笨的打分**（只数共享多少个词，不看向量/BM25/IDF/重排），
  **32/35 条题就能把 gold 排到第 1 名**，领先优势中位只有 3 个词。

也就是说 gold 的优势全靠**主题词**撑着 —— 而主题词之所以在问题里，
是因为**题目是用条款的措辞写的**（覆盖比中位 61%）。

于是本文件做一件事：**把提问换成员工自己会说的话**，让主题词从问题里消失。

--------------------------------------------------------------------------
为什么是「同一批答案的另一种问法」，而不是新写一批题
--------------------------------------------------------------------------
这是本实验**能不能得出有效结论**的关键。如果新题考的是新的事实，
那么新旧两批的分数差异就说不清是"问法变了"还是"考点变了"。

所以每道 D 题都**绑定一道 A 题作为对照组**（字段 `control_case_key`），并且：

    reference（参考答案）· evidence（原文依据）· doc_slugs（gold 文档）
    这三个字段**一个字都不手写** —— 直接从对照组原样复制（见 `resolve()`）。

好处有两个，都是"不靠人自觉"的：
  ① 抄错不可能发生（手抄 10 条 evidence 是必然会出错的事）
  ② **"唯一变量是提问措辞"这件事可以被机器证明** —— 验证脚本只要断言
     "D 题的 reference/evidence/doc_slugs 与对照组逐字相同"即可。
     ⚠ 这是**对照组实验的基本要求**：变量必须只有一个，而且要能证明它只有一个。

--------------------------------------------------------------------------
改写规格（三条硬约束）
--------------------------------------------------------------------------
  ① **删掉与 gold 篇主题同名的词**（年休假 / 住宿费 / 采购 / 密码 / 更换周期…）
  ② **保留判定所需的事实**（"工作 6 年"必须留着 —— 它是"5 天"唯一成立的理由）
  ③ **答案必须仍然唯一**（不能含糊到跨越档位，否则就是制造歧义，不是制造难度）

⚠ ① 和 ② 是**互相拉扯**的：很多题的判定事实本身就长在主题词上
   （"住宿费标准"必须先知道是一线城市还是地级市）。
   所以本批 10 条的预期是「能改到达标的只有一部分」—— 这不是失败，
   它就是这条路的产出上限，靠零 token 量名次来确认，不靠感觉。

--------------------------------------------------------------------------
为什么字段名叫 `challenge` 而不是 `hard`
--------------------------------------------------------------------------
"难"是个相对词，写进名字里就再也说不清它相对谁。
`challenge` 表达的是**它的设计意图**：用来挑战检索配置的区分能力。
它到底难不难，由实测的「gold 在融合序里的名次」说了算（见 d24_rank_probe）。
"""

from typing import Any

from scripts.d21_cases import CASES as _BASE_CASES

# ============================================================
# 10 条挑战题：每条都绑定一道 A 题作为对照
# ============================================================
# 字段说明：
#   case_key          挑战题编号（D 开头，与 A/B/C 三个字母段分开）
#   control_case_key  对照组（A 题编号）—— reference / evidence / doc_slugs 从它复制
#   question          **本批唯一的自变量**：口语化的问法
#   removed          设计说明：从原问题里删掉了哪些词（给人看的，便于复核）
#   kept             设计说明：保留了哪个判定事实（它是答案唯一性的依据）
#   risk_note         我自评的风险（有就写，没有就不写）—— 不下结论，只是提示复核重点
_CHALLENGE_SPECS: list[dict[str, Any]] = [
    {
        "case_key": "D01",
        "control_case_key": "A01",
        "question": "我毕业就进公司了，到现在一共工作了 6 年，一年能休多少天假？",
        "removed": "年休假 / 带薪 / 累计工作已满 1 年不满 10 年",
        "kept": "「工作 6 年」—— 落在 1~10 年这一档，答案 5 天唯一",
        "risk_note": "「假」字可能被读成病假或事假；但「一年能休多少天」指向年度固定额度，仍唯一",
    },
    {
        "case_key": "D04",
        "control_case_key": "A04",
        "question": "我今年还剩几天假没休完，最多能留几天到明年？必须赶在什么时候之前用掉？",
        "removed": "年休假 / 结转",
        "kept": "「留到明年」= 结转、「最多几天」= 5 天、「什么时候之前」= 次年 3 月 31 日",
        "risk_note": "语料条款的前提是「因工作任务需要未能使用完毕」，本问法省掉了这个前提；结论不变",
    },
    {
        "case_key": "D08",
        "control_case_key": "A08",
        "question": "我出差那几天，公司另外每天给多少钱吃饭？",
        "removed": "伙食补助 / 标准",
        "kept": "「出差」「每天」「吃饭」—— 指向伙食补助，答案 100 元/天唯一",
        "risk_note": "语料另有「当日往返按 50 元」「对方供餐不计发」两个分支；本问法说「那几天」暗示过夜，但不排除被读成 50 元",
    },
    {
        "case_key": "D09",
        "control_case_key": "A09",
        "question": "我们要买一批 3 万块钱的东西，得走谁的审批？",
        "removed": "单次采购金额 / 部门和人员审批",
        "kept": "「3 万」—— 落在 5000~50000 档，答案（部门经理审核 → 采购部门会同财务部门审批）唯一",
        "risk_note": "「3 万」这个数字同时会命中差旅（报销超 20000 元）与付款（5 万以下）两篇 —— 这正是我们要的竞争",
    },
    {
        "case_key": "D12",
        "control_case_key": "A12",
        "question": "我登录办公系统用的密码，公司要求设多长？多久必须换一次？",
        "removed": "系统密码 / 长度 / 更换周期",
        "kept": "「登录系统用的密码」= 系统账号、「多长」= 12 位、「多久换一次」= 90 天",
        "risk_note": "语料另有一条「不得在多个系统使用同一密码」，同篇内会竞争 —— 同篇竞争按文档级去重后不扣分（名次取最早）",
    },
    {
        "case_key": "D14",
        "control_case_key": "A14",
        "question": "要是我发现公司的数据可能泄露了，我得在多久之内告诉谁？",
        "removed": "信息安全事件 / 当事人 / 向哪个部门报告",
        "kept": "「数据泄露」「多久之内」「告诉谁」—— 指向 2 小时 / 信息安全管理部门",
        "risk_note": "「数据」「泄露」会同时命中《数据分级》的多条条款 —— 竞争度高，是本批的重点观察对象",
    },
    {
        "case_key": "D17",
        "control_case_key": "A17",
        "question": "我一年有几次机会能提晋升？一般是什么时候？",
        "removed": "晋升评审窗口 / 设置 / 月份",
        "kept": "「一年」「几次」「提晋升」「什么时候」—— 指向两次（3 月、9 月）",
        "risk_note": "语料中晋升还有「任职满 12 个月」的年限要求，本问法没提；但问的是「几次机会」，答案仍唯一为两次",
    },
    {
        "case_key": "D19",
        "control_case_key": "A19",
        "question": "我这台公司发的笔记本用得有点卡了，按规定多久能换新的？",
        "removed": "办公电脑 / 配发 / 更换周期",
        "kept": "「公司发的笔记本」= 配发的办公设备、「多久能换」= 更换周期 4 年",
        "risk_note": "「卡了」可能被读成故障报修（IT 服务台篇），而语料里「无法修复需提前更换」是另一条分支；本问法用「按规定多久」把它拉回周期条款",
    },
    {
        "case_key": "D21",
        "control_case_key": "A21",
        "question": "我们每个人一年至少要上多少小时的内部课？",
        "removed": "参加 / 内部培训 / 时间要求 / 学时",
        "kept": "「一年」「多少小时」「内部课」—— 指向不少于 24 学时",
        "risk_note": "语料原文用的是「学时」而不是「小时」，模型可能答「24 小时」；参考答案仍是原文，不受影响",
    },
    {
        "case_key": "D24",
        "control_case_key": "A24",
        "question": "我要付给供应商 6 万块钱，这笔得谁签字？",
        "removed": "单笔付款金额 / 审批",
        "kept": "「付给供应商 6 万」= 单笔付款超过 50000 元，答案（财务负责人审核后报总经理）唯一",
        "risk_note": "「6 万」同时会命中采购条款（超 5 万报总经理）—— 两篇的最终审批人都是总经理，但审核链路不同，是本批的重点观察对象",
    },
]

# 对照组编号 → 原题（用来复制 evidence / reference / doc_slugs）
_BASE_BY_KEY: dict[str, dict] = {c["case_key"]: c for c in _BASE_CASES}


def resolve() -> list[dict]:
    """
    把 10 条规格展开成**可用于评测的完整题目**。

    为什么 evidence / reference / doc_slugs 要从对照组复制而不是手写：
      ① 手抄 10 条原文依据必然出错，而且错了不会报错（同 D21 的 B07 事故：
         引文标错了文档，导致那题根本不是跨文档题，B 类成绩全程是假的）
      ② 「唯一变量是提问措辞」这句话必须**可被机器证明**。
         复制过来的字段逐字相同，验证脚本就能断言这一点；
         手写的话，只能靠"我记得我抄对了"。

    Raises:
        KeyError: 对照组编号不存在（写错编号时必须当场炸，不能静默跳过 ——
                  静默跳过会让这条题拿着错误的 gold 去算名次，结论全废）
    """
    out: list[dict] = []
    for spec in _CHALLENGE_SPECS:
        base = _BASE_BY_KEY[spec["control_case_key"]]
        out.append(
            {
                "case_key": spec["case_key"],
                # category 用新名字而不是 doc_qa：它是一类**独立的题**，
                # 混进 doc_qa 会让旧口径的 A 类均分在不知情的情况下被拉低
                "category": "challenge_qa",
                # difficulty 与对照组保持一致 —— 两批逐行可比（本批的"难"在检索层，
                # 不在人工标注的那个 difficulty 上；且实测该标签与难度只弱相关）
                "difficulty": base["difficulty"],
                "question": spec["question"],
                # ↓↓↓ 以下四个字段一律从对照组复制，一个字都不手写 ↓↓↓
                "reference": base["reference"],
                "evidence": base["evidence"],
                "doc_slugs": list(base["doc_slugs"]),
                "expected_tool": base.get("expected_tool"),
                "is_negative": False,
                # ↓↓↓ 以下三个字段只给人看，不参与评测，也不进指纹 ↓↓↓
                "control_case_key": spec["control_case_key"],
                "removed": spec["removed"],
                "kept": spec["kept"],
                "risk_note": spec.get("risk_note", ""),
            }
        )
    return out


CHALLENGE_CASES: list[dict] = resolve()


def summarize() -> dict:
    """分布统计（给探针和验证脚本用）。"""
    by_slug: dict[str, int] = {}
    for c in CHALLENGE_CASES:
        for s in c["doc_slugs"]:
            by_slug[s] = by_slug.get(s, 0) + 1
    return {
        "total": len(CHALLENGE_CASES),
        "controls": [c["control_case_key"] for c in CHALLENGE_CASES],
        "covered_slugs": sorted(by_slug),
        "with_risk_note": sum(1 for c in CHALLENGE_CASES if c["risk_note"]),
    }


if __name__ == "__main__":
    # 自检必须写成 assert：只用 print 的话，数字不对时脚本照样退出 0，
    # 等于"自检恒过"——这正是 D11 那条"空列表恒满足"的同族问题。
    import json

    print(json.dumps(summarize(), ensure_ascii=False, indent=2))

    keys = [c["case_key"] for c in CHALLENGE_CASES]
    assert len(set(keys)) == len(keys), f"题号有重复：{keys}"
    assert all(k.startswith("D") for k in keys), f"挑战题编号必须是 D 开头：{keys}"
    for c in CHALLENGE_CASES:
        assert c["evidence"], f"{c['case_key']} 缺 evidence"
        assert c["reference"], f"{c['case_key']} 缺 reference"
        assert c["doc_slugs"], f"{c['case_key']} 缺 doc_slugs（没有 gold 就算不了名次）"
        assert c["control_case_key"] in _BASE_BY_KEY, f"{c['case_key']} 的对照组不存在"
        # 唯一变量是问法 —— 这四个字段必须与对照组逐字相同（本实验的地基）
        base = _BASE_BY_KEY[c["control_case_key"]]
        assert c["evidence"] == base["evidence"], f"{c['case_key']} 的 evidence 被改动过"
        assert c["reference"] == base["reference"], f"{c['case_key']} 的 reference 被改动过"
        assert c["doc_slugs"] == list(base["doc_slugs"]), f"{c['case_key']} 的 doc_slugs 被改动过"
        assert c["question"] != base["question"], f"{c['case_key']} 的提问与对照组一字未改（那不是挑战题）"

    print(f"\n自检通过：{len(keys)} 条，全部绑定到存在的对照组，且答案为原样复制。")
