"""
工具服务器接口
================
5 个出口，对应"用户上传工具"这一整件事：

    GET    /api/tool-servers                    列表（持久状态 + 运行状态）
    POST   /api/tool-servers                    上传（multipart：file + name + 描述）
    POST   /api/tool-servers/{id}/reconnect     重连（代码改了/容器崩了/开机没起来）
    PATCH  /api/tool-servers/{id}               启用 / 停用
    DELETE /api/tool-servers/{id}               删除（断开 + 摘工具 + 删代码 + 删记录）

与 `/api/tools` 的分工（两个接口很像，但不是一回事）：
    · `/api/tools`        —— "**现在能调什么**"：注册表视角，含内置工具与 MCP server
    · `/api/tool-servers` —— "**我传过什么、它该不该跑**"：管理视角
    一个工具 server 起不来时，前者少几个工具，后者多一条 failed —— 两边都要能看见。

为什么上传返回 **202** 而不是 201：
    "已接受"不等于"已可用"。沙箱可能起来了但握手失败（代码里没写 MCP server），
    此时记录与文件都已落库落盘，用户能在列表里看到它、改它、删它。
    201 会让前端以为"成了"，然后对着一个空工具列表发懵。
"""

import uuid

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile, status

from app.config import settings
from app.schemas.tool_server import (
    ToolCallRequest,
    ToolServerListResponse,
    ToolServerOut,
    ToolServerUpdate,
)
from app.services import tool_server_service
from app.services.tool_server_service import MAX_CODE_BYTES, ToolServerError

router = APIRouter(tags=["tool-servers"])


def _http(exc: ToolServerError) -> HTTPException:
    """
    service 的异常 → HTTP 异常。

    为什么要这层薄转换：service 层不 import fastapi（它要能被脚本与单测直接用），
    所以它只带一个 `status_code` 类属性；由接口层负责"翻译成 HTTP"。
    """
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get(
    "/tool-servers",
    response_model=ToolServerListResponse,
    summary="用户上传的工具 server 列表",
)
async def list_tool_servers_api() -> dict:
    servers = await tool_server_service.list_all()
    return {"total": len(servers), "servers": servers}


@router.post(
    "/tool-servers",
    response_model=ToolServerOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传一个 MCP Server 程序（.py）",
)
async def upload_tool_server_api(
    file: UploadFile = File(..., description="MCP Server 源码，只接受 .py"),
    name: str = Form(..., description="机器名（3-32 位小写字母/数字/下划线，作工具名前缀）"),
    display_name: str = Form("", description="显示名（留空 = 用 name）"),
    description: str = Form("", description="说明（只给人看，不发给 LLM）"),
) -> dict:
    """
    上传 → 校验 → 落盘 → 入库 → 拉起，一次请求做完。

    ⚠ **先读上限 +1 字节，再判断是否超限**。
      不能 `await file.read()` 之后再看大小 —— 那样 100MB 的文件已经全进内存了，
      大小限制拦不住内存占用（这是 RAG 上传链路踩过的同一个坑）。
    """
    if not settings.tool_sandbox_enabled:
        # 在读取请求体之前就拒绝：省一次无谓的上传，也让原因更明确。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="工具沙箱未启用（TOOL_SANDBOX_ENABLED=false）",
        )

    content = await file.read(MAX_CODE_BYTES + 1)
    if len(content) > MAX_CODE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"文件过大：上限 {MAX_CODE_BYTES} 字节（256KB）",
        )

    try:
        return await tool_server_service.create(
            name=name,
            display_name=display_name,
            description=description,
            filename=file.filename or "",
            content=content,
        )
    except ToolServerError as e:
        raise _http(e) from e


@router.post(
    "/tool-servers/{tool_id}/reconnect",
    response_model=ToolServerOut,
    summary="重新拉起一台工具 server",
)
async def reconnect_tool_server_api(tool_id: uuid.UUID) -> dict:
    try:
        return await tool_server_service.reconnect(tool_id)
    except ToolServerError as e:
        raise _http(e) from e


@router.patch(
    "/tool-servers/{tool_id}",
    response_model=ToolServerOut,
    summary="启用 / 停用一台工具 server",
)
async def update_tool_server_api(tool_id: uuid.UUID, payload: ToolServerUpdate) -> dict:
    """
    停用的语义是"断开并摘掉工具"，不是"删除"：
    代码还在、记录还在，LLM 看不到这些工具。重新启用时不需重传。
    """
    try:
        return await tool_server_service.set_status(tool_id, payload.status)
    except ToolServerError as e:
        raise _http(e) from e


@router.post(
    "/tool-servers/{tool_id}/call",
    summary="试跑这台 server 的一个工具",
)
async def call_tool_api(tool_id: uuid.UUID, payload: ToolCallRequest) -> dict:
    """
    与 Agent 调用走**同一条**路径（`execute_tool` → stub → 沙箱）。

    错误一律以工具输出的形式返回（`execute_tool` 的契约：失败也是字符串），
    只有"工具不存在/不属于这台 server"才是 404。
    这个区分很重要：工具内部报错是**用户代码的事**，不该被当成接口错误。
    """
    try:
        return await tool_server_service.call_tool(tool_id, payload.tool, payload.arguments)
    except ToolServerError as e:
        raise _http(e) from e


@router.delete(
    "/tool-servers/{tool_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除一台工具 server",
)
async def delete_tool_server_api(tool_id: uuid.UUID) -> Response:
    try:
        await tool_server_service.delete(tool_id)
    except ToolServerError as e:
        raise _http(e) from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)
