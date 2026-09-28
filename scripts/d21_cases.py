"""
D21 评测集：50 条（A 文档问答 30 含 5 负例 / B 跨文档推理 10 / C 工具调用 10）
===============================================================================
本文件是**评测集的数据源**。`scripts/d21_seed.py` 读它写入 `eval_cases` 表，
写完即冻结（算 sha256 指纹）。**改一个字就要重算指纹、重跑 D23**。

--------------------------------------------------------------------------
三条出题纪律（每条都对应一个字段，能机检 —— 不靠人自觉）
--------------------------------------------------------------------------
① **答案必须落在同一篇的同一个切片内**（A 类）
   → 字段 `doc_slugs` 只有一个元素。违反的话这题其实是 B 类题，
     会把 A 类的分数**拉低**，而 A 是消融的基线 —— 直接扭曲 (C−A)/A。

② **参考答案必须有原文依据**（防止凭空捏造）
   → 字段 `evidence`，取自语料**单行内**的连续片段，必须**逐字**命中。
     如果参考答案里的数字（"年假 5 天"）在语料里根本不存在，这题永远答不对、
     **而且不会报错**，只会在 D23 报告里表现为"某个配置特别差"。

③ **负例的"正确"是拒答**
   → `is_negative=True` + `evidence` 写成 `[不存在] 关键词…`。
     这比"我读过语料，确实没有"强得多：机检会去语料里搜这些关键词，
     搜到了就说明这题**根本不是负例**——那 5 条幻觉控制的测量当场作废。

--------------------------------------------------------------------------
`evidence` 的三种形态（由 `d21_eval_verify.py` 分别校验）
--------------------------------------------------------------------------
    正向证据   "原文片段"                     → 必须逐字出现在语料中
    多段（B 类）"片段1 | 片段2"                → 第 i 段必须出现在第 i 个 doc_slug 的文档里
                                               （这同时证明了"这题**真的**跨了这两篇"）
    负例       "[不存在] 关键词A|关键词B"        → 每个关键词在整个语料里都搜不到
    工具题     "[工具] search_documents"        → 与 expected_tool 字段一致

--------------------------------------------------------------------------
`expected_tool` 的 `__none__` 哨兵值（为什么不用 NULL）
--------------------------------------------------------------------------
C 类题里有"**不该调用任何工具**"这一类（闲聊/常识）。它和"这个字段没填"是
两件完全不同的事 —— 用 NULL 表示前者，会被读成后者（同族问题见 D19：
`similarity=None` 被渲染成"相关度 None"）。所以显式给一个哨兵值。
"""

# ============================================================
# A 类：文档问答（30 条 = 25 正例 + 5 负例）
# ============================================================
_A_CASES: list[dict] = [
    {
        "case_key": "A01", "category": "doc_qa", "difficulty": "easy",
        "question": "员工累计工作满 1 年但不满 10 年的，每年可以享受多少天带薪年休假？",
        "reference": "每年享受带薪年休假 5 天。",
        "evidence": "已满 1 年不满 10 年的，每年享受带薪年休假 5 天",
        "doc_slugs": ["annual-leave"],
    },
    {
        "case_key": "A02", "category": "doc_qa", "difficulty": "easy",
        "question": "申请年休假时，最小的请假单位是多少天？",
        "reference": "最小请假单位为 0.5 天。",
        "evidence": "最小请假单位为 0.5 天",
        "doc_slugs": ["annual-leave"],
    },
    {
        "case_key": "A03", "category": "doc_qa", "difficulty": "hard",
        "question": "员工要连续请 5 个工作日年假，需要谁审批？要提前多久提交申请？",
        "reference": "连续请假超过 3 个工作日的，需由部门经理审核后报分管副总经理审批；"
                     "并且应当至少提前 5 个工作日提交申请。",
        "evidence": "连续请假超过 3 个工作日的，需由部门经理审核后报分管副总经理审批",
        "doc_slugs": ["annual-leave"],
    },
    {
        "case_key": "A04", "category": "doc_qa", "difficulty": "medium",
        "question": "当年没用完的年休假最多可以结转多少天？必须在什么时间之前用完？",
        "reference": "最多可结转 5 天至次年，结转部分应当在次年 3 月 31 日前使用完毕，逾期作废。",
        "evidence": "最多可结转 5 天至次年，结转部分应当在次年 3 月 31 日前使用完毕，逾期作废",
        "doc_slugs": ["annual-leave"],
    },
    {
        "case_key": "A05", "category": "doc_qa", "difficulty": "easy",
        "question": "出差到省会城市，住宿费的报销标准是多少？",
        "reference": "一线城市（含直辖市、省会城市及计划单列市）住宿费标准为每人每晚不超过 500 元。",
        "evidence": "一线城市的住宿费标准为每人每晚不超过 500 元",
        "doc_slugs": ["travel-expense"],
    },
    {
        "case_key": "A06", "category": "doc_qa", "difficulty": "medium",
        "question": "出差距离 500 公里，可以乘坐飞机吗？对舱位有什么要求？",
        "reference": "城际出行距离超过 300 公里的可以乘坐飞机，舱位为经济舱。",
        "evidence": "距离超过 300 公里的，可以乘坐飞机经济舱",
        "doc_slugs": ["travel-expense"],
    },
    {
        "case_key": "A07", "category": "doc_qa", "difficulty": "medium",
        "question": "出差结束后多久必须提交报销申请？如果超过了这个时限会怎么处理？",
        "reference": "应当在出差结束后 15 个工作日内提交报销申请；超过时限的应当说明理由"
                     "并经部门经理批准，无正当理由的可以不予报销。",
        "evidence": "员工应当在出差结束后 15 个工作日内提交报销申请",
        "doc_slugs": ["travel-expense"],
    },
    {
        "case_key": "A08", "category": "doc_qa", "difficulty": "easy",
        "question": "出差期间的伙食补助标准是多少？",
        "reference": "每人每天 100 元，按实际出差天数计算；当日往返的出差按每天 50 元计算。",
        "evidence": "标准为每人每天 100 元，按实际出差天数计算",
        "doc_slugs": ["travel-expense"],
    },
    {
        "case_key": "A09", "category": "doc_qa", "difficulty": "easy",
        "question": "单次采购金额 30000 元，需要经过哪些部门和人员审批？",
        "reference": "单次采购金额在 5000 元以上至 50000 元的，由部门经理审核后，"
                     "经采购部门会同财务部门审批。",
        "evidence": "单次采购金额在 5000 元以上至 50000 元的，由部门经理审核后，经采购部门会同财务部门审批",
        "doc_slugs": ["procurement"],
    },
    {
        "case_key": "A10", "category": "doc_qa", "difficulty": "hard",
        "question": "供应商年度评价得分低于多少分会被列入观察名单？连续两年低于该分数会有什么后果？",
        "reference": "评价得分低于 60 分列入观察名单；连续两年低于 60 分的，取消其合格供应商资格。",
        "evidence": "评价得分低于 60 分的供应商列入观察名单",
        "doc_slugs": ["procurement"],
    },
    {
        "case_key": "A11", "category": "doc_qa", "difficulty": "easy",
        "question": "采购物资到货后，使用部门应当在几个工作日内完成验收？",
        "reference": "应当在 5 个工作日内组织验收。",
        "evidence": "使用部门应当在 5 个工作日内组织验收",
        "doc_slugs": ["procurement"],
    },
    {
        "case_key": "A12", "category": "doc_qa", "difficulty": "easy",
        "question": "公司对系统密码的长度和更换周期有什么要求？",
        "reference": "密码长度不得少于 12 位，且应当每 90 天更换一次。",
        "evidence": "密码长度不得少于 12 位",
        "doc_slugs": ["infosec"],
    },
    {
        "case_key": "A13", "category": "doc_qa", "difficulty": "medium",
        "question": "向外部单位提供机密级数据，需要经过谁审批？",
        "reference": "应当经信息安全委员会审批，并与接收方签订保密协议。",
        "evidence": "向外部单位提供机密级数据的，应当经信息安全委员会审批",
        "doc_slugs": ["infosec"],
    },
    {
        "case_key": "A14", "category": "doc_qa", "difficulty": "medium",
        "question": "发现信息安全事件后，当事人必须在多长时间内向哪个部门报告？",
        "reference": "应当在 2 小时内向信息安全管理部门报告，不得隐瞒不报或者迟报。",
        "evidence": "当事人应当在 2 小时内向信息安全管理部门报告",
        "doc_slugs": ["infosec"],
    },
    {
        "case_key": "A15", "category": "doc_qa", "difficulty": "medium",
        "question": "绩效考核分为哪几个等级？其中 S 级和 A 级的比例上限分别是多少？",
        "reference": "分为 S、A、B、C、D 五个等级；S 级比例不超过参评人数的 10%，"
                     "A 级比例不超过参评人数的 30%。",
        "evidence": "S 级为卓越，比例不超过参评人数的 10%；A 级为优秀，比例不超过参评人数的 30%",
        "doc_slugs": ["performance"],
    },
    {
        "case_key": "A16", "category": "doc_qa", "difficulty": "medium",
        "question": "员工连续两个季度考核结果为 C 级，公司会启动什么流程？该流程持续多久？",
        "reference": "启动绩效改进计划，计划期限为 60 天，期满经评估仍未达到要求的，"
                     "公司可依法调整其岗位或者解除劳动合同。",
        "evidence": "绩效改进计划期限为 60 天",
        "doc_slugs": ["performance"],
    },
    {
        "case_key": "A17", "category": "doc_qa", "difficulty": "easy",
        "question": "公司每年设置有几次晋升评审窗口？分别在哪个月份？",
        "reference": "每年设置两次晋升评审窗口，分别为 3 月和 9 月。",
        "evidence": "公司每年设置两次晋升评审窗口，分别为 3 月和 9 月",
        "doc_slugs": ["performance"],
    },
    {
        "case_key": "A18", "category": "doc_qa", "difficulty": "medium",
        "question": "IT 故障分为哪几个级别？其中影响全公司业务的故障应当在多长时间内响应？",
        "reference": "分为 P1、P2、P3 三个级别；P1 级为影响全公司或者核心业务系统的故障，"
                     "服务台应当在 15 分钟内响应。",
        "evidence": "P1 级为影响全公司或者核心业务系统的故障，服务台应当在 15 分钟内响应",
        "doc_slugs": ["it-support"],
    },
    {
        "case_key": "A19", "category": "doc_qa", "difficulty": "easy",
        "question": "公司配发的办公电脑，更换周期是多久？",
        "reference": "办公电脑的更换周期为 4 年，自设备配发之日起计算。",
        "evidence": "办公电脑的更换周期为 4 年，自设备配发之日起计算",
        "doc_slugs": ["it-support"],
    },
    {
        "case_key": "A20", "category": "doc_qa", "difficulty": "hard",
        "question": "公司业务系统数据的备份保留多久？员工的重要工作文件应当存放在哪里？",
        "reference": "备份数据保留 90 天；员工的重要工作文件应当存放于公司文件服务器"
                     "或者云文档平台，不得仅保存在本地终端。",
        "evidence": "备份数据保留 90 天",
        "doc_slugs": ["it-support"],
    },
    {
        "case_key": "A21", "category": "doc_qa", "difficulty": "easy",
        "question": "每位员工每年参加内部培训的时间要求是多少学时？",
        "reference": "每年参加内部培训的时间不少于 24 学时。",
        "evidence": "每位员工每年参加内部培训的时间不少于 24 学时",
        "doc_slugs": ["training"],
    },
    {
        "case_key": "A22", "category": "doc_qa", "difficulty": "medium",
        "question": "员工报名参加费用为 15000 元的外部培训，需要经过哪些审批？",
        "reference": "单次培训费用超过 10000 元的，由部门经理审核后，"
                     "经人力资源部复核并报分管副总经理审批。",
        "evidence": "单次培训费用超过 10000 元的，由部门经理审核后，经人力资源部复核并报分管副总经理审批",
        "doc_slugs": ["training"],
    },
    {
        "case_key": "A23", "category": "doc_qa", "difficulty": "easy",
        "question": "公司每个月设定哪几天为集中付款日？",
        "reference": "每月 10 日和 25 日两个集中付款日，遇法定节假日顺延至节假日后第一个工作日。",
        "evidence": "公司设定每月 10 日和 25 日两个集中付款日",
        "doc_slugs": ["finance-payment"],
    },
    {
        "case_key": "A24", "category": "doc_qa", "difficulty": "medium",
        "question": "一笔 60000 元的付款需要谁审批？",
        "reference": "单笔付款金额超过 50000 元的，由财务负责人审核后报总经理审批。",
        "evidence": "单笔付款金额超过 50000 元的，由财务负责人审核后报总经理审批",
        "doc_slugs": ["finance-payment"],
    },
    {
        "case_key": "A25", "category": "doc_qa", "difficulty": "medium",
        "question": "增值税专用发票需要在多长时间内完成认证？逾期未认证会有什么后果？",
        "reference": "应当在开票之日起 90 天内完成认证；逾期未认证会造成税款损失，"
                     "因个人原因导致逾期未认证的，相关损失由责任人承担。",
        "evidence": "取得的增值税专用发票应当在开票之日起 90 天内完成认证",
        "doc_slugs": ["finance-payment"],
    },
    # ---- 负例 5 条：正确行为是「拒答」，不是「答得像那么回事」 ----
    {
        "case_key": "A26", "category": "doc_qa", "difficulty": "medium", "is_negative": True,
        "question": "公司对员工子女的教育补贴是怎么规定的？",
        "reference": "知识库中没有关于员工子女教育补贴的任何规定，无法回答该问题。",
        "evidence": "[不存在] 教育补贴|子女教育|教育津贴",
        "doc_slugs": [],
    },
    {
        "case_key": "A27", "category": "doc_qa", "difficulty": "medium", "is_negative": True,
        "question": "公司提供班车服务或者交通补贴吗？",
        "reference": "知识库中没有关于班车服务或交通补贴的规定，无法回答该问题。",
        "evidence": "[不存在] 班车|交通补贴",
        "doc_slugs": [],
    },
    {
        "case_key": "A28", "category": "doc_qa", "difficulty": "medium", "is_negative": True,
        "question": "员工可以带宠物到公司上班吗？",
        "reference": "知识库中没有关于携带宠物上班的规定，无法回答该问题。",
        "evidence": "[不存在] 宠物",
        "doc_slugs": [],
    },
    {
        "case_key": "A29", "category": "doc_qa", "difficulty": "medium", "is_negative": True,
        "question": "公司的年会一般在什么时间举办？",
        "reference": "知识库中没有关于年会举办时间的规定，无法回答该问题。",
        "evidence": "[不存在] 年会",
        "doc_slugs": [],
    },
    {
        "case_key": "A30", "category": "doc_qa", "difficulty": "medium", "is_negative": True,
        "question": "公司有没有员工股权激励计划？具体是怎么规定的？",
        "reference": "知识库中没有关于员工股权激励计划的规定，无法回答该问题。",
        "evidence": "[不存在] 股权激励|期权",
        "doc_slugs": [],
    },
]

# ============================================================
# B 类：跨文档推理（10 条）—— 答案需要 ≥2 篇文档的信息
# ============================================================
# evidence 分两段，用 " | " 分隔，**第 i 段必须出现在第 i 个 doc_slug 的文档里**。
# 这条约束能机检出"这题其实是单文档题"——那种题会让 B 类退化成 A 类，
# 白占 10 条额度（出题纪律 ① 的 B 类版本）。
_B_CASES: list[dict] = [
    {
        "case_key": "B01", "category": "cross_doc", "difficulty": "medium",
        "question": "员工出差结束后，从提交报销申请到款项实际到账，公司规定的时限总共是多少？",
        "reference": "出差结束后 15 个工作日内提交报销申请；报销单据审核通过后，"
                     "款项最长不超过 15 个工作日到账。两项相加，最长约 30 个工作日。",
        "evidence": "员工应当在出差结束后 15 个工作日内提交报销申请"
                    " | 报销款项最长不超过 15 个工作日到账",
        "doc_slugs": ["travel-expense", "finance-payment"],
    },
    {
        "case_key": "B02", "category": "cross_doc", "difficulty": "medium",
        "question": "员工参加外部培训期间的住宿费按什么标准执行？",
        "reference": "外部培训费用中的差旅费按公司差旅费用管理办法执行；"
                     "住宿费标准为一线城市每人每晚不超过 500 元、其他地级市不超过 400 元、"
                     "县级市及以下不超过 300 元。",
        "evidence": "差旅费的列支范围和标准按照公司差旅费用管理办法执行"
                    " | 一线城市的住宿费标准为每人每晚不超过 500 元",
        "doc_slugs": ["training", "travel-expense"],
    },
    {
        "case_key": "B03", "category": "cross_doc", "difficulty": "hard",
        "question": "公司要采购一批 8 万元的设备，采购环节和之后的付款环节分别需要谁审批？",
        "reference": "采购金额超过 50000 元的，由采购部门、财务部门审核后报总经理审批；"
                     "付款金额超过 50000 元的，由财务负责人审核后报总经理审批。"
                     "两个环节最终都需要总经理审批。",
        "evidence": "单次采购金额超过 50000 元的，由采购部门、财务部门审核后报总经理审批"
                    " | 单笔付款金额超过 50000 元的，由财务负责人审核后报总经理审批",
        "doc_slugs": ["procurement", "finance-payment"],
    },
    {
        "case_key": "B04", "category": "cross_doc", "difficulty": "hard",
        "question": "请假对绩效考核有哪些影响？相关的天数门槛分别是多少？",
        "reference": "全年累计事假超过 15 天的，不参加当年度的绩效考核评优；"
                     "当季度累计请假超过 30 天的（不含年休假和法定节假日），不参与当季度绩效评级。",
        "evidence": "全年累计事假超过 15 天的，不参加当年度的绩效考核评优"
                    " | 当季度累计请假超过 30 天的，请假天数不含年休假和法定节假日",
        "doc_slugs": ["annual-leave", "performance"],
    },
    {
        "case_key": "B05", "category": "cross_doc", "difficulty": "hard",
        "question": "公司为一名员工支付了 25000 元的外部培训费用，这笔培训需要经过哪些审批？"
                    "培训期间的差旅怎么处理？公司可以要求员工承担什么义务？",
        "reference": "培训费用超过 10000 元的，由部门经理审核后经人力资源部复核并报分管副总经理审批；"
                     "培训期间差旅费按公司差旅费用管理办法执行；费用超过 20000 元的应签订"
                     "培训服务期协议，服务期 2 年。",
        "evidence": "单次培训费用超过 10000 元的，由部门经理审核后，经人力资源部复核并报分管副总经理审批"
                    " | 员工参加外部培训、外部会议的，其差旅费用按照本办法执行",
        "doc_slugs": ["training", "travel-expense"],
    },
    {
        "case_key": "B06", "category": "cross_doc", "difficulty": "medium",
        "question": "发生影响全公司的系统故障时，安全事件上报和 IT 故障响应各自的时限要求是什么？",
        "reference": "信息安全事件当事人应当在 2 小时内向信息安全管理部门报告；"
                     "P1 级 IT 故障（影响全公司或核心业务系统）服务台应当在 15 分钟内响应。",
        "evidence": "当事人应当在 2 小时内向信息安全管理部门报告，不得隐瞒不报或者迟报"
                    " | P1 级为影响全公司或者核心业务系统的故障，服务台应当在 15 分钟内响应",
        "doc_slugs": ["infosec", "it-support"],
    },
    {
        "case_key": "B07", "category": "cross_doc", "difficulty": "medium",
        "question": "采购的物资到货之后，验收和付款分别在什么时间完成？",
        "reference": "使用部门应当在到货后 5 个工作日内组织验收并出具验收单；"
                     "付款由财务部门在每月 10 日和 25 日两个集中付款日办理（遇节假日顺延）。",
        "evidence": "使用部门应当在 5 个工作日内组织验收"
                    " | 公司设定每月 10 日和 25 日两个集中付款日",
        "doc_slugs": ["procurement", "finance-payment"],
    },
    {
        "case_key": "B08", "category": "cross_doc", "difficulty": "medium",
        "question": "新员工入职后，公司在办公设备和系统账号方面分别是怎么安排的？",
        "reference": "公司为新入职员工配发办公电脑一台、显示器一台及键盘鼠标一套；"
                     "系统账号由所在部门提出申请，经信息安全管理部门审批后开通。",
        "evidence": "公司为新入职员工配发办公电脑一台，标准配置为商用笔记本电脑，配备显示器一台、键盘鼠标一套"
                    " | 员工入职后由所在部门提出账号权限申请，经信息安全管理部门审批后开通",
        "doc_slugs": ["it-support", "infosec"],
    },
    {
        "case_key": "B09", "category": "cross_doc", "difficulty": "medium",
        "question": "员工参加外部会议的审批要求和费用处理分别是怎么规定的？",
        "reference": "参加外部会议应当提前 10 个工作日提交申请，由部门经理审核后报分管副总经理审批；"
                     "会议产生的差旅费用按公司差旅费用管理办法执行。",
        "evidence": "员工参加外部会议应当提前 10 个工作日提交申请"
                    " | 员工参加外部培训、外部会议的，其差旅费用按照本办法执行",
        "doc_slugs": ["training", "travel-expense"],
    },
    {
        "case_key": "B10", "category": "cross_doc", "difficulty": "hard",
        "question": "关于预付款比例，采购管理办法和财务付款管理办法的规定一致吗？分别是多少？",
        "reference": "两处规定一致。采购管理办法规定预付款原则上不超过合同金额的 30%；"
                     "财务付款管理办法同样规定预付款比例原则上不超过合同金额的 30%。",
        "evidence": "预付款原则上不超过合同金额的 30%"
                    " | 预付款比例原则上不超过合同金额的 30%",
        "doc_slugs": ["procurement", "finance-payment"],
    },
]

# ============================================================
# C 类：工具调用（10 条）—— 测"选对工具"和"不乱调工具"
# ============================================================
# ⚠ 现状约束（实测）：系统当前只注册了 2 个工具 —— current_time / search_documents。
#   PRD F7.1 的示例 `query_inventory`（库存）属于 D26/D27 才接入的 MCP 工具，
#   所以"参数构造"这个维度**现在测不实**，本组只能覆盖：
#     ① 该检索知识库（4 条）② 该取当前时间（3 条）③ 两个都不该调（3 条）
#   报告里必须如实标注这个缺口，不能假装 C 类已完整覆盖。
_NO_TOOL = "__none__"      # 哨兵值：**显式要求不调用任何工具**（区别于"未标注"）


def _tool_case(key: str, question: str, tool: str, why: str) -> dict:
    """工具类题的构造器：reference 说明期望行为，evidence 标记期望工具（供机检）。"""
    if tool == _NO_TOOL:
        reference = f"不应当调用任何工具，直接回答即可。理由：{why}"
    else:
        reference = f"应当调用 {tool} 工具完成该请求。理由：{why}"
    return {
        "case_key": key, "category": "tool_call", "difficulty": "easy",
        "question": question, "reference": reference,
        "evidence": f"[工具] {tool}",
        "doc_slugs": [], "expected_tool": tool,
    }


_C_CASES: list[dict] = [
    _tool_case("C01", "公司员工每年有几天带薪年休假？", "search_documents",
               "答案在知识库制度文档里，必须先检索"),
    _tool_case("C02", "出差住宿费的报销标准是多少？", "search_documents",
               "答案在知识库制度文档里，必须先检索"),
    _tool_case("C03", "公司的密码多久需要更换一次？", "search_documents",
               "答案在知识库制度文档里，必须先检索"),
    _tool_case("C04", "供应商年度评价低于多少分会被列入观察名单？", "search_documents",
               "答案在知识库制度文档里，必须先检索"),
    _tool_case("C05", "现在几点了？", "current_time",
               "问的是当前时刻，属于实时信息，知识库里没有"),
    _tool_case("C06", "今天是几月几号？", "current_time",
               "问的是当前日期，属于实时信息，知识库里没有"),
    _tool_case("C07", "帮我确认一下现在的时间。", "current_time",
               "问的是当前时刻，属于实时信息，知识库里没有"),
    _tool_case("C08", "你好，你都能做些什么？", _NO_TOOL,
               "闲聊兼能力询问，没有需要外部信息的成分，调工具反而多余"),
    _tool_case("C09", "1 加 1 等于几？", _NO_TOOL,
               "纯常识计算，模型自己就会，不需要检索也不需要取时间"),
    _tool_case("C10", "谢谢你，回答得挺清楚的。", _NO_TOOL,
               "礼貌性回应，不包含任何信息需求"),
]

# ============================================================
# 汇总（顺序固定：A → B → C，指纹依赖这个顺序）
# ============================================================
def _normalize(raw: dict) -> dict:
    """
    补齐可选字段的默认值。

    为什么要显式补：SQLAlchemy 里 `None` 与"字段没给"在写入时表现不同，
    而指纹计算要遍历全部字段 —— 缺键会让指纹随"这条题有没有写 expected_tool"
    而变化，属于**假报警**来源（内容没改，指纹却变了）。
    """
    case = dict(raw)
    case.setdefault("is_negative", False)
    case.setdefault("expected_tool", None)
    case.setdefault("doc_slugs", [])
    return case


CASES: list[dict] = [_normalize(c) for c in (_A_CASES + _B_CASES + _C_CASES)]


def summarize() -> dict:
    """分布统计（seed 脚本会打印，验证脚本会断言）。"""
    by_category: dict[str, int] = {}
    by_difficulty: dict[str, int] = {}
    slugs: set[str] = set()
    for c in CASES:
        by_category[c["category"]] = by_category.get(c["category"], 0) + 1
        by_difficulty[c["difficulty"]] = by_difficulty.get(c["difficulty"], 0) + 1
        slugs.update(c["doc_slugs"])
    return {
        "total": len(CASES),
        "by_category": by_category,
        "by_difficulty": by_difficulty,
        "negative": sum(1 for c in CASES if c["is_negative"]),
        "covered_slugs": sorted(slugs),
    }
