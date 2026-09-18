"""
文档管理接口的数据结构（D12）
==============================
上传 / 列表 / 删除三个接口共用 DocumentOut。

注意模型的 id 是 uuid.UUID（Python 侧），这里也声明成 UUID ——
pydantic 会把它序列化成字符串返回给前端，不用手动 str() 转换。
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DocumentOut(BaseModel):
    """一份文档的对外表示。"""

    # from_attributes=True：允许直接用 ORM 对象构造（读 document.filename）
    # 没有它就必须手写一层「document → dict」的转换代码，多一处可能写错的地方
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="文档ID")
    filename: str = Field(description="原始文件名")
    file_type: str = Field(description="文件类型：pdf/docx/md/txt")
    status: str = Field(
        description=(
            "入库状态：processing=后台处理中（此刻检索不到）；"
            "ready=切片已全部入库，可被检索；failed=失败，原因见 error_message"
        )
    )
    chunk_count: int = Field(description="切片数（ready 后才有意义）")
    error_message: str | None = Field(default=None, description="失败原因（status=failed 时）")
    created_at: datetime = Field(description="上传时间")
