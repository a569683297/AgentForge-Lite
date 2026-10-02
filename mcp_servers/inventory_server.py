"""
MCP Server：库存查询（自研，D26 正式产物）
============================================
这是本项目**第一台自己写的 MCP Server** —— 一个**独立进程**，
通过 stdin/stdout 上的 JSON-RPC 报文对外提供工具，不依赖项目里的任何代码。

设计约束（三条，都是为了 D26 的验收项「服务独立可调」）：
1. **零项目依赖**：只 import 官方 `mcp` 包 + Python 标准库。
   不 import `app.*`、不连 Postgres、不读 `.env` —— 所以它能在任意目录、任意 Python 环境里单独跑起来。
   （反过来说：如果它必须 import 项目的配置才能启动，那它就不叫"独立服务"，只是项目的一部分。）
2. **数据在进程内**：`_INVENTORY` 是写死在文件里的内存 mock 数据，没有数据库、没有外部接口。
3. **绝不往 stdout 打业务日志**：stdout 是**协议专用通道**，上面跑的必须是 JSON-RPC 报文。
   （D26 实测过：`mcp 2.2.0` 的 `MCPServer.run()` 会把 fd 1 改道到 fd 2，所以 `print` 不会污染协议 ——
   但那是 SDK 给的**保护**，不是协议的**许可**。换个 SDK 或自己手写就没有这层保护了。
   所以这里的规矩是：要打日志就显式写 stderr。）

运行方式（就是一个普通 Python 脚本，不需要任何框架）：
    python mcp_servers/inventory_server.py
起了之后它阻塞在 stdin 上，等客户端发报文；管道一关（stdin EOF）它就退出。
"""

import sys
from typing import Any

from mcp.server import MCPServer

# ── 建盒子：第一个位置参数是这个 server 自己的名字（实测：传什么，握手时就报什么） ──
server = MCPServer("inventory")

# ── 内存 mock 数据：12 条电子元器件库存 ──
# 刻意造出四种库存状态，方便演示过滤：
#   0（缺货）/ 极低（<50）/ 偏低（<300）/ 充足（>1000）
# 字段含义：sku=物料编码（唯一标识）、category=品类、stock=当前库存数量、
#          unit_price=单价（元）、warehouse=所在仓库货位
_INVENTORY: list[dict[str, Any]] = [
    {"sku": "RES-0603-10K", "name": "贴片电阻 10kΩ 0603", "category": "电阻",
     "stock": 12000, "unit_price": 0.01, "warehouse": "A-01"},
    {"sku": "RES-0805-1K", "name": "贴片电阻 1kΩ 0805", "category": "电阻",
     "stock": 8600, "unit_price": 0.01, "warehouse": "A-01"},
    {"sku": "CAP-0603-100N", "name": "贴片电容 100nF 0603", "category": "电容",
     "stock": 15400, "unit_price": 0.02, "warehouse": "A-02"},
    {"sku": "CAP-1210-10U", "name": "贴片电容 10µF 1210", "category": "电容",
     "stock": 320, "unit_price": 0.35, "warehouse": "A-02"},
    {"sku": "MCU-STM32F103", "name": "MCU STM32F103C8T6", "category": "芯片",
     "stock": 47, "unit_price": 12.80, "warehouse": "B-01"},
    {"sku": "MCU-ESP32-S3", "name": "MCU ESP32-S3-WROOM-1", "category": "芯片",
     "stock": 0, "unit_price": 18.50, "warehouse": "B-01"},
    {"sku": "ADC-ADS1256", "name": "24 位 ADC ADS1256", "category": "芯片",
     "stock": 18, "unit_price": 89.00, "warehouse": "B-02"},
    {"sku": "CON-XH2-4P", "name": "连接器 XH2.54 4P", "category": "连接器",
     "stock": 2400, "unit_price": 0.18, "warehouse": "C-01"},
    {"sku": "CON-DB9-F", "name": "DB9 母座 焊接式", "category": "连接器",
     "stock": 96, "unit_price": 2.40, "warehouse": "C-01"},
    {"sku": "PCB-FR4-2L", "name": "PCB 打样 FR4 双层", "category": "PCB",
     "stock": 25, "unit_price": 45.00, "warehouse": "D-01"},
    {"sku": "PWR-LM2596", "name": "降压模块 LM2596", "category": "电源",
     "stock": 210, "unit_price": 3.20, "warehouse": "A-03"},
    {"sku": "PWR-AMS1117-33", "name": "LDO AMS1117-3.3", "category": "电源",
     "stock": 5300, "unit_price": 0.25, "warehouse": "A-03"},
]

# 供 docstring 引用的品类清单（与上面数据保持同步；写成常量是为了别在两处手写同一串字）
_CATEGORIES = "、".join(dict.fromkeys(row["category"] for row in _INVENTORY))


# ⚠ 注意下面这个 docstring —— 它不只是注释：
#    @server.tool() 会把它取出来，当作工具描述写进 tools/list 的报文里发给客户端。
#    而客户端会把它转给 LLM，LLM 就是靠这段话判断"什么情况下该调这个工具"。
#    所以这里写的是「什么时候用」，不是「内部怎么实现」。
@server.tool()
def query_inventory(
    min_stock: int | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """查询库存里的电子元器件。

    当用户问"某某物料还有多少库存""哪些料该补货了""某个品类的库存情况"时使用。
    两个参数都可以不传：都不传 = 返回全部（按库存从少到多排序）。

    Args:
        min_stock: 只看库存**不少于**这个数的物料。传 0 或省略 = 不过滤。
        category: 只看某个品类。可选值：电阻、电容、芯片、连接器、PCB、电源；省略 = 不过滤。

    Returns:
        匹配的物料列表，每项含 sku / name / category / stock / unit_price / warehouse。
        按 stock 升序排列（库存最少的排最前，方便一眼看出哪些该补货）。
        没有匹配项时返回空列表 —— 空结果不是错误。
    """
    rows = _INVENTORY
    if category is not None:
        rows = [row for row in rows if row["category"] == category]
    if min_stock is not None:
        rows = [row for row in rows if row["stock"] >= min_stock]
    return sorted(rows, key=lambda row: row["stock"])


if __name__ == "__main__":
    # 启动探针：显式写 stderr（不碰 stdout，见文件顶部第 3 条规矩）。
    print(f"[inventory-server] 已启动，共 {len(_INVENTORY)} 条物料；等待 stdin 报文…",
          file=sys.stderr, flush=True)

    # run() 默认就是 stdio，这里显式写出来避免歧义。
    # 这一行会一直阻塞 —— stdin 上收到 EOF（客户端断开）才返回。
    server.run(transport="stdio")
