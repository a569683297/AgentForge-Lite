"""
模型注册表接口（模型切换需求 S1）
==================================
6 个出口（前端"模型切换器 + 配置弹窗"的全部数据面）：

    GET    /api/models             列表：内置（.env 有 key 的）+ 自定义（DB）
    POST   /api/models             新建自定义模型
    PATCH  /api/models/{id}        编辑（api_key 留空 = 不修改，见 schemas/model.py）
    DELETE /api/models/{id}        删除（204）
    POST   /api/models/test        连通性测试 —— 对**表单里还没保存**的值
    POST   /api/models/{id}/test   连通性测试 —— 对**已保存**的模型

为什么测试要拆成两个接口（而不是一个）：
    "还没保存就测"是配置弹窗里最常见的动作（先验证再保存，避免存一堆错配置），
    此时没有 id 可引用，只能把表单值整个传上来；
    而列表里"测一下这条已有的"又不该要求前端先查出完整配置（还要处理打码回传的坑）。
    两个入口、同一段服务层逻辑（model_service.test_connection）。

安全：本文件**不 import 任何 key 的字面量**；所有出口值来自 service 的打码转换
（api_key_masked），明文 key 只在 service → 网关那一侧使用（S2 接）。
"""

import uuid

from fastapi import APIRouter, HTTPException, Response, status

from app.schemas.model import (
    LlmModelCreate,
    LlmModelOut,
    LlmModelUpdate,
    ModelListResponse,
    ModelTestRequest,
    ModelTestResult,
)
from app.services import model_service

router = APIRouter(tags=["models"])


@router.get("/models", response_model=ModelListResponse, summary="模型列表（内置 + 自定义）")
async def list_models_api() -> dict:
    """返回 `{"models": [...]}`；每项含 kind（builtin/local/custom）与打码后的 key。"""
    return {"models": await model_service.list_models()}


@router.post(
    "/models",
    response_model=LlmModelOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建自定义模型",
)
async def create_model_api(payload: LlmModelCreate) -> dict:
    return await model_service.create_model(payload)


@router.patch("/models/{model_id}", response_model=LlmModelOut, summary="编辑自定义模型")
async def update_model_api(model_id: uuid.UUID, payload: LlmModelUpdate) -> dict:
    """
    `model_id` 声明为 UUID：非法值（如 "not-a-uuid"）由 FastAPI 直接 422 ——
    与 ChatRequest.session_id 同一手法（D14 的结论：类型交给 pydantic，不自己写正则）。
    """
    result = await model_service.update_model(model_id, payload)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型不存在")
    return result


@router.delete(
    "/models/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除自定义模型",
)
async def delete_model_api(model_id: uuid.UUID) -> Response:
    """
    只有"删除成功"和"没这条"两种结局 → 204 / 404。
    （"被会话选中却被删"的连带处理在前端：它发现选中项没了就回退默认 —— 见需求稿 §5.2。）
    """
    if not await model_service.delete_model(model_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/models/test", response_model=ModelTestResult, summary="连通性测试（表单未保存值）")
async def test_unsaved_model_api(payload: ModelTestRequest) -> dict:
    """
    对表单里当前的填写值测一次 —— 配错时当场知道，而不是保存后发一条正式消息试错。
    失败返回 `ok=false + error`（200 响应，见 ModelTestResult 的说明）。
    """
    return await model_service.test_connection(payload.base_url, payload.api_key, payload.model)


@router.post("/models/{model_id}/test", response_model=ModelTestResult, summary="连通性测试（已保存模型）")
async def test_saved_model_api(model_id: uuid.UUID) -> dict:
    """对库里已保存的模型测一次（前端配置弹窗列表里的"测试"按钮）。"""
    row = await model_service.get_model(model_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型不存在")
    return await model_service.test_connection(row.base_url, row.api_key, row.model)
