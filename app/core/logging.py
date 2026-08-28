"""
结构化日志模块
==============
统一日志格式（JSON 友好），业务代码用 `from app.core.logging import logger`。

面试考点：真实公司项目必须可观测 —— 日志是排障第一手段，
统一入口保证格式一致，后续接 ELK/Loki 零成本。
"""

import logging
import sys

from app.config import settings


def setup_logging() -> logging.Logger:
    """配置根 logger，返回项目 logger。"""
    fmt = (
        "%(asctime)s | %(levelname)-8s | %(name)s | "
        "%(filename)s:%(lineno)d | %(message)s"
    )
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format=fmt,
        stream=sys.stdout,          # Docker 里 stdout 才能被收集
        force=True,                 # 覆盖任何已有配置
    )
    return logging.getLogger("agentforge")


# 模块级单例：业务代码直接 import 使用
logger = setup_logging()
