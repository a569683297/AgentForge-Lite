"""
模型注册表的数据结构（模型切换需求 S1）
========================================
对应 app/api/models.py 的请求/响应体。

两条贯穿全文件的约定（改动时先读这两条）：

① **base_url 一律规范化**：去首尾空白 + **去尾斜杠**。
   为什么必须去尾斜杠：网关拼接请求地址是 `f"{base_url}/chat/completions"` ——
   用户填 `https://api.x.com/v1/` 时会拼出 `.../v1//chat/completions`（双斜杠）。
   多数服务器容忍，但**不保证**；与其赌服务器宽容，不如写入时就归一。
   规范化发生在**校验器**里 → 入库的值永远是规范形态，读出来直接用。

② **api_key 在 PATCH 里"留空 = 不修改"**（需求稿 §3.2 C5）：
   前端编辑时只回显打码值、不回显明文，所以"把 key 字段提交回空"必须是
   "不动原来的 key"，否则每次编辑都会把 key 清掉。
   实现判断在 service 层（`if payload.api_key:` —— None 和 "" 都是 falsy）。
"""

import uuid

from pydantic import BaseModel, Field, field_validator


def _normalize_base_url(value: str) -> str:
    """去空白 + 去尾斜杠 + 必须是 http(s) 完整地址。"""
    normalized = value.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise ValueError("base_url 必须是 http:// 或 https:// 开头的完整地址")
    return normalized


class LlmModelCreate(BaseModel):
    """POST /api/models 的请求体：新建自定义模型。"""

    name: str = Field(min_length=1, max_length=255, description="显示名，如「我的 Qwen」")
    base_url: str = Field(
        max_length=500,
        description="OpenAI 兼容端点根地址（写入时自动去尾斜杠），如 https://api.example.com/v1",
    )
    api_key: str | None = Field(
        default=None,
        max_length=500,
        description="API Key；本地端点（如 Ollama）不需要时留空",
    )
    model: str = Field(min_length=1, max_length=255, description="模型标识，如 qwen-max")

    @field_validator("name", "model")
    @classmethod
    def _strip_not_blank(cls, value: str) -> str:
        """显示名/模型标识去空白；全是空白视为没填（拒绝）。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能是空白字符串")
        return stripped

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return _normalize_base_url(value)


class LlmModelUpdate(BaseModel):
    """
    PATCH /api/models/{id} 的请求体：编辑。

    全部字段可选（只传要改的那些）。
    ⚠ `api_key` 不传、传 None、传 "" 都表示**不修改**（见模块 docstring 约定 ②）。
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = Field(default=None, description="启用开关")

    @field_validator("name", "model")
    @classmethod
    def _strip_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能是空白字符串")
        return stripped

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_base_url(value)


class LlmModelOut(BaseModel):
    """
    列表/新建/编辑的响应项。

    `kind` 三值（见需求稿 §1.2）：builtin=内置（.env）· local=自定义但指向本机
    （localhost / host.docker.internal 等）· custom=其余自定义。
    local 由 base_url 的**主机名**自动判定 —— 这是有真实依据的分类，
    不是商业平台的"免费/折扣"那种编出来的标签。
    """

    id: str = Field(description="内置模型是通道名（如 deepseek）；自定义模型是 UUID")
    name: str = Field(description="显示名")
    model: str = Field(description="模型标识")
    kind: str = Field(description="builtin | local | custom")
    enabled: bool
    base_url: str | None = Field(default=None, description="端点根地址（展示用）")
    api_key_masked: str | None = Field(
        default=None,
        description="打码后的 key（如 sk-***c4）。**永远不返回明文**",
    )


class ModelListResponse(BaseModel):
    """GET /api/models 的响应体。"""

    models: list[LlmModelOut]


class ModelTestRequest(BaseModel):
    """POST /api/models/test 的请求体：对**表单里还没保存**的配置做连通性测试。"""

    base_url: str = Field(max_length=500, description="端点根地址")
    api_key: str | None = Field(default=None, max_length=500)
    model: str = Field(min_length=1, max_length=255)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return _normalize_base_url(value)


class ModelTestResult(BaseModel):
    """
    连通性测试结果。

    失败**也是一种正常返回**（ok=false + error 是给人看的人话），
    不是 HTTP 错误 —— 因为"这个配置通不通"正是接口要回答的问题本身，
    用 4xx/5xx 表达会让前端要把同一件事分两处处理。
    """

    ok: bool = Field(description="是否连通")
    latency_ms: int | None = Field(default=None, description="成功时的往返耗时（毫秒）")
    error: str | None = Field(default=None, description="失败原因（已翻译成人话）")
    sample: str | None = Field(default=None, description="成功时模型回复的片段（证明它在回话）")
