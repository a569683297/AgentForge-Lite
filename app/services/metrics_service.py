"""
语义层服务（D37 / PRD §9.10 · F11）
====================================

一句话：**LLM 不写 SQL，只选「指标名 + 维度 + 时间窗」；SQL 由 metrics.yaml 的
模板渲染，维度必须命中白名单，时间参数走绑定。**

为什么值得单独一层（面试考点）：NL2SQL 的翻车点（模型生成的 SQL 跑错 / 跑废 /
越权）不是靠"事后加校验"堵住的，而是靠**根本不让它生成 SQL** 消掉的。
语义层就是"口径的唯一来源"，也是 Agent 与数据源之间的一层翻译。

三条防护（PRD §9.10.2）在本模块的落点：
    ① 指标名白名单 —— get_spec() 不在 METRIC_REGISTRY 中的名字直接拒绝
    ② 维度白名单   —— render_sql() 校验 dimension ∈ spec.dimensions；
                      {dimension} 占位符**只会**被白名单里的原文替换
    ③ 时间参数绑定 —— :start / :end 走 SQLAlchemy 绑定参数，不做字符串拼接

端到端流程（一次指标查询）：
    run_metric("tool_usage", dimension="tool_name", start="2026-09-01", end="2026-10-16")
      ① 查注册表（防护①）→ ② 渲染模板前校验维度（防护②）→ ③ 渲染出 SQL
      ④ 解析时间窗（北京时间自然日 → 左闭右开的带时区边界）
      ⑤ 按 source 分派：sql → 项目库执行；langfuse_api → 取数层 + query()（块 2）
      ⑥ 行归一化（date / Decimal → JSON 安全值）→ 统一结构返回
    两个源的出口返回同一个 dict schema（含 degraded 标记）—— 上层对数据来自
    哪里无感知（PRD §9.10.5 约束 1），唯一例外是 langfuse 源可整体降级（约束 3）。

两条口径约定（写在这里，全项目只此一处）：
    · **业务时区 = Asia/Shanghai**：日期窗口指北京时间自然日 [start, end] 闭区间。
      实测 PG 会话时区是 Etc/UTC —— 直接把 'YYYY-MM-DD' 丢给 SQL，日界会悄悄
      变成北京时间的 08:00（"今天"少 8 小时）。所以边界在 Python 侧显式换算，
      按日聚合也在 SQL 里 AT TIME ZONE 显式截断，全程不依赖会话时区。
    · **rows 结构统一**：`[{"dim": ..., "value": ...}, ...]`；聚合指标（无维度）
      返回单行且没有 dim 列。两种数据源返回结构完全一致（PRD §9.10.5 约束 1），
      上层（Agent / 前端）对数据来自哪里无感知。

不做的事（边界要清楚）：
    · 不缓存：SQL 源快且新；Langfuse 源的 60s 缓存由**取数层**负责（D28 已实现，
      query() 直接复用）—— 缓存放在离数据源近的地方，语义层只管口径
    · 不做权限（单用户演示系统，PRD 范围如此）
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import text

from app.core.db import async_session_factory
from app.core.logging import logger

# 白名单常量从取数层导入（**单向依赖**：语义层 → langfuse_client；
# 反向引用只存在于 langfuse_client.query() 的函数内延迟导入里），
# 保证"YAML 加载期校验"与"运行时聚合校验"用的是**同一份**定义。
from app.services.langfuse_client import SUPPORTED_AGGS, SUPPORTED_FIELDS

# ---- 业务时区（全项目唯一出处，见模块头"两条口径约定"）----
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

# ---- metrics.yaml 位置：项目根目录（口径文件的唯一事实来源）----
METRICS_YAML_PATH = Path(__file__).resolve().parents[2] / "metrics.yaml"

# 允许出现在 sql_template 里的合法占位符，**有且仅有** {dimension}。
# 为什么要在加载期做这个检查：像 `{start}` 这种笔误若漏到运行期，
# 渲染结果是一句带花括号的非法 SQL，报错现场离"真正的错误"（写错的 YAML）很远。
_ALLOWED_PLACEHOLDER = "{dimension}"
_PLACEHOLDER_RE = re.compile(r"\{[^}]*\}")

# 日期严格格式：先过正则再进 fromisoformat。
# 为什么要正则兜底：Python 3.11 起 `date.fromisoformat("20261001")`（无分隔）也能解析，
# 但 API / 工具的参数说明写的都是 YYYY-MM-DD —— 校验口径必须与文档口径一致，
# 否则"文档说只收一种格式、代码收好几种"就是又一处静默不一致。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

VALID_SOURCES = ("sql", "langfuse_api")


class MetricsError(Exception):
    """
    语义层领域错误：指标不存在 / 维度非法 / 日期窗口非法。

    单独定义而不是复用 ValueError：调用方（API 层、Agent 工具）需要把
    "用户参数问题"与"系统故障"分开处理 —— 前者回 400 / 回给 LLM 自行纠正，
    后者才该进错误监控。错误消息一律写成**可据以行动**的形式（列出可用取值）。
    """


@dataclass(frozen=True)
class MetricSpec:
    """一个指标的口径定义（由 metrics.yaml 加载而来，运行期不可变）。"""

    name: str
    label: str
    description: str
    source: str                                # "sql" | "langfuse_api"
    dimensions: tuple[str, ...] = ()           # 分组维度白名单（空 = 聚合指标）
    sql_template: str | None = None            # source == "sql" 时必有
    table: str | None = None                   # 元信息：主要来源表（文档/审计用）
    time_column: str | None = None             # 元信息：时间列
    endpoint: str | None = None                # source == "langfuse_api" 时用（块 2）
    query: dict[str, Any] | None = None        # source == "langfuse_api" 的查询参数（块 2）


# ============================================================
# 加载 + 加载期校验（写错就启动失败，不留到运行期）
# ============================================================
def _load_registry(path: Path = METRICS_YAML_PATH) -> dict[str, MetricSpec]:
    """
    读取 metrics.yaml 并逐项校验，返回 {指标名: MetricSpec}。

    校验规则（每条都对应一次"如果不查会怎样"）：
      · source 只允许 sql / langfuse_api —— 拼错的 source 会让分派静默走错分支
      · sql 源必须有非空 sql_template     —— 空模板 = 查询必然失败，且报错在远端
      · 模板里的占位符**只有** {dimension} —— 见模块头 _ALLOWED_PLACEHOLDER 说明
      · 声明了维度 ⇔ 模板用了 {dimension}  —— 两者不一致时：要么渲染漏做替换、
        要么占位符没人替换，两种都是"到运行期才炸"的静默缺口
      · langfuse 源（块 2）：endpoint 非空 + query 是 dict + agg/field 命中取数层
        白名单 + group_by 属于 dimensions —— 这四项写错的话，跑查询才炸/静默少数据；
        另外两个源**互斥的配置段**（sql 源带 query 段 / langfuse 源带 sql_template）
        也被拒 —— 那是复制粘贴 YAML 时最典型的误写，而误写的配置会被静默忽略。
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_metrics: dict[str, Any] = raw.get("metrics") or {}
    if not raw_metrics:
        raise MetricsError(f"metrics.yaml 未定义任何指标：{path}")

    specs: dict[str, MetricSpec] = {}
    for name, item in raw_metrics.items():
        item = item or {}
        source = item.get("source")
        if source not in VALID_SOURCES:
            raise MetricsError(
                f"指标 {name} 的 source 非法：{source!r}（只允许 {' / '.join(VALID_SOURCES)}）"
            )
        dimensions = tuple(item.get("dimensions") or ())
        template = item.get("sql_template") or None

        if source == "sql":
            if not template:
                raise MetricsError(f"指标 {name} 是 sql 源，但 sql_template 为空")
            if item.get("query"):
                raise MetricsError(
                    f"指标 {name} 是 sql 源，却带着 query 段（那是 langfuse_api 源的配置）"
                    " —— 检查是不是复制粘贴时改了 source 没删配置"
                )
            placeholders = set(_PLACEHOLDER_RE.findall(template)) - {_ALLOWED_PLACEHOLDER}
            if placeholders:
                raise MetricsError(
                    f"指标 {name} 的 sql_template 含非法占位符 {sorted(placeholders)}；"
                    f"只允许 {_ALLOWED_PLACEHOLDER}（时间参数用 :start / :end 绑定）"
                )
            if dimensions and _ALLOWED_PLACEHOLDER not in template:
                raise MetricsError(f"指标 {name} 声明了维度 {list(dimensions)}，但模板里没有 {_ALLOWED_PLACEHOLDER}")
            if not dimensions and _ALLOWED_PLACEHOLDER in template:
                raise MetricsError(f"指标 {name} 未声明维度，模板里却出现了 {_ALLOWED_PLACEHOLDER}")

        elif source == "langfuse_api":
            # 块 2：langfuse 源的四条校验（每条都对应一种"运行期才炸 / 静默少数据"的写法）
            if not item.get("endpoint"):
                raise MetricsError(f"指标 {name} 是 langfuse_api 源，但 endpoint 为空")
            if template:
                raise MetricsError(
                    f"指标 {name} 是 langfuse_api 源，却带着 sql_template"
                    " —— 检查是不是复制粘贴时改了 source 没删模板"
                )
            q = item.get("query")
            if not isinstance(q, dict):
                raise MetricsError(
                    f"指标 {name} 的 query 必须是键值映射（YAML 里写成嵌套 dict），"
                    f"收到：{type(q).__name__}"
                )
            if q.get("agg") not in SUPPORTED_AGGS:
                raise MetricsError(
                    f"指标 {name} 的 query.agg 非法：{q.get('agg')!r}；"
                    f"允许：{' / '.join(SUPPORTED_AGGS)}"
                )
            if q.get("field") not in SUPPORTED_FIELDS:
                raise MetricsError(
                    f"指标 {name} 的 query.field 非法：{q.get('field')!r}；"
                    f"允许：{sorted(SUPPORTED_FIELDS)}"
                )
            group_by = q.get("group_by")
            if group_by is not None and group_by not in dimensions:
                raise MetricsError(
                    f"指标 {name} 的 query.group_by（{group_by!r}）不在 dimensions"
                    f" 白名单 {list(dimensions)} 里"
                )

        specs[name] = MetricSpec(
            name=name,
            label=str(item.get("label") or name),
            description=str(item.get("description") or ""),
            source=source,
            dimensions=dimensions,
            sql_template=template,
            table=item.get("table"),
            time_column=item.get("time_column"),
            endpoint=item.get("endpoint"),
            query=item.get("query"),
        )
    # 按名字排序返回：清单接口 / 工具目录的输出顺序稳定，便于对账
    return dict(sorted(specs.items()))


# 模块级加载一次。为什么不做懒加载：口径文件是**代码级**的事实来源 ——
# 写错了应该在进程启动时就炸（fail fast），而不是等第一个请求进来才发现。
METRIC_REGISTRY: dict[str, MetricSpec] = _load_registry()


# ============================================================
# 防护②③：模板渲染与时间窗解析（纯函数，可单测，不碰库）
# ============================================================
def render_sql(spec: MetricSpec, dimension: str | None) -> str:
    """
    把 sql_template 渲染成可执行 SQL —— 安全的关键就在这个函数的两个分支里：

      · 有维度的指标：dimension 必须**逐字**命中白名单，替换仅发生在这个前提下。
        注入串（"config_name; DROP TABLE ..."）根本匹配不上白名单 → 直接拒绝，
        **永远到不了数据库**（这不是"过滤危险字符"，是"只放行已知安全值"）。
      · 聚合指标：dimension 必须为 None。传了维度说明调用方把指标理解错了，
        静默忽略会让它拿着错误假设继续跑 —— 直接报错并说明该指标不支持分组。
    """
    if spec.source != "sql":
        raise MetricsError(f"指标 {spec.name} 不是 sql 源（source={spec.source}），不能渲染 SQL")
    if spec.dimensions:
        if dimension not in spec.dimensions:
            raise MetricsError(
                f"指标 {spec.name} 不支持的维度：{dimension!r}；"
                f"可用维度：{list(spec.dimensions)}"
            )
        return (spec.sql_template or "").replace(_ALLOWED_PLACEHOLDER, dimension)
    # 无维度指标 + 非 None 维度：报错；空串在 run_metric 入口已归一为 None
    if dimension is not None:
        raise MetricsError(f"指标 {spec.name} 是聚合指标，不支持分组维度：{dimension!r}")
    return spec.sql_template or ""


def parse_window(start: str, end: str) -> tuple[datetime, datetime]:
    """
    解析日期窗口：'YYYY-MM-DD' 字符串（北京时间自然日闭区间）→ 左闭右开的时间戳边界。

    返回 (start_dt, end_dt) 两个**带时区**的 datetime：
        start_dt = start 当日 00:00（北京）        —— SQL 里用 created_at >= :start
        end_dt   = end   次日 00:00（北京，开区间） —— SQL 里用 created_at <  :end
    闭区间语义（含 end 当天）由"次日 00:00 + 严格小于"实现 ——
    用 <= end 当日 23:59:59 会有"这一秒内的数据怎么办"的缝隙，"次日零点开区间"没有。
    """
    for field_name, value in (("start_date", start), ("end_date", end)):
        if not isinstance(value, str) or not _DATE_RE.match(value):
            raise MetricsError(f"{field_name} 格式必须是 YYYY-MM-DD，收到：{value!r}")
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError as exc:  # 形如 2026-13-01 / 2026-02-30：格式对但日期不存在
        raise MetricsError(f"日期不合法：{exc}") from exc
    if start_d > end_d:
        raise MetricsError(f"start_date（{start}）晚于 end_date（{end}），窗口为空")
    return (
        datetime.combine(start_d, time.min, tzinfo=BUSINESS_TZ),
        datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=BUSINESS_TZ),
    )


# ============================================================
# 执行：查注册表 → 校验 → 渲染 → 分派 → 归一化
# ============================================================
def get_spec(name: str) -> MetricSpec:
    """防护①：按名字取指标定义；不在注册表中直接拒绝（错误消息列出全部可用指标）。"""
    spec = METRIC_REGISTRY.get(name)
    if spec is None:
        available = "、".join(f"{n}（{s.label}）" for n, s in METRIC_REGISTRY.items())
        raise MetricsError(f"未知指标：{name!r}；可用指标：{available}")
    return spec


def list_metrics() -> list[dict]:
    """指标清单（GET /api/metrics 与 Agent 工具目录共用）。"""
    return [
        {
            "name": spec.name,
            "label": spec.label,
            "description": spec.description,
            "source": spec.source,
            "dimensions": list(spec.dimensions),
        }
        for spec in METRIC_REGISTRY.values()
    ]


def _jsonable(value: Any) -> Any:
    """把驱动返回的原生对象归一为 JSON 安全值（date→ISO 串、Decimal→float）。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


async def _fetch_sql(sql: str, params: dict[str, Any]) -> list[dict]:
    """在项目库执行渲染后的 SQL（参数走绑定），返回按行归一化的 dict 列表。"""
    async with async_session_factory() as session:
        result = await session.execute(text(sql), params)
        return [{k: _jsonable(v) for k, v in row._mapping.items()} for row in result]


async def run_metric(
    metric: str,
    dimension: str | None = None,
    start: str = "",
    end: str = "",
) -> dict[str, Any]:
    """
    对外主入口：按「指标名 + 维度 + 时间窗」查询，统一结构返回。

    返回：{"metric", "dimension", "source", "rows", "degraded", "error"}
    三种"用户参数错误"（未知指标 / 维度非法 / 日期非法）都抛 MetricsError，
    调用方据此回 400 或把消息交回 LLM —— 错误消息本身就是给 LLM 的纠错提示。

    ⚠ 唯一不抛错的失败：Langfuse 源不可用（网络/凭据/限流）。此时返回
      degraded=True + error 说明 + rows=[] —— **只降级该指标**（PRD §9.10.5 约束 3）：
      外部系统挂了不等于这次查询"失败"，前端该渲染"暂不可用"占位、
      Agent 该转述一句人话，两者都不需要理解异常。SQL 源不受它影响。
    """
    spec = get_spec(metric)

    # 空串归一为 None：工具调用里 LLM 常把"不分组"写成 ""，不该因此报错；
    # 空白串同理。真正的非法维度（不在白名单）仍然会被 render_sql / query 拒绝。
    dimension = (dimension or "").strip() or None

    start_dt, end_dt = parse_window(start, end)

    degraded = False
    error: str | None = None

    if spec.source == "sql":
        sql = render_sql(spec, dimension)
        rows = await _fetch_sql(sql, {"start": start_dt, "end": end_dt})
    elif spec.source == "langfuse_api":
        from app.services.langfuse_client import (
            LangfuseUnavailableError,
            get_langfuse_client,
        )

        try:
            rows = await get_langfuse_client().query(spec, dimension, start, end)
        except LangfuseUnavailableError as exc:
            logger.warning("指标降级：%s 读通道不可用：%s", spec.name, exc)
            rows, degraded, error = [], True, str(exc)[:300]
    else:  # pragma: no cover —— 加载期已校验 source 合法，这里只是防御
        raise MetricsError(f"指标 {spec.name} 的数据源暂不支持：{spec.source}")

    return {
        "metric": spec.name,
        "dimension": dimension,
        "source": spec.source,
        "rows": rows,
        "degraded": degraded,
        "error": error,
    }
