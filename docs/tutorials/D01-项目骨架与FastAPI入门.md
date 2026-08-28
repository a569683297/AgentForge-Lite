# D1 复习教程：项目骨架、FastAPI 与配置中心

> 日期：2026-08-28 ｜ 项目：AgentForge 阶段1 ｜ 状态：已验收通过

---

## 1. 今日目标

学完这一天，你应该能独立回答：

1. 一次 HTTP 请求进入 FastAPI 后经历了什么（入口 → 处理 → 返回）
2. 为什么所有配置要集中在一个类里读 `.env`，而不是到处 `os.environ`
3. 为什么 API key 所在的 `.env` 绝不能提交 git
4. `lifespan` 里的代码在什么时机执行、干什么用
5. 项目为什么要做"三层解耦"目录（api / services / core）

---

## 2. 核心概念讲解

### 2.1 FastAPI 是什么？（类比 Hono）

**一句话**：FastAPI 是 Python 界的 Hono/Express——一个帮你处理 HTTP 请求的 Web 框架。

**你已会的**：`app.get('/health', handler)` 注册一个 URL 处理器。
**FastAPI 写法**：
```python
@router.get("/health")        # 装饰器 = 注册路由，等价于 app.get('/health')
async def health() -> dict:   # 处理函数，等价于 handler
    return {...}              # 返回 dict 自动转成 JSON
```

**关键差异**（要记）：
- Python 用**装饰器**（`@`）注册路由，TS 用链式调用
- FastAPI 用**类型注解**（`-> dict`）做响应校验和文档生成，TS 用 zod/interface
- `async def` 表示异步函数——你写 TS 的 `async function` 一样，await 一个意思

### 2.2 一次请求的生命周期（端到端）

```
curl http://localhost:8000/api/health
        │
        ▼
uvicorn（HTTP 服务器，监听 8000 端口）
        │  收到请求
        ▼
FastAPI 路由匹配：找 /api/health 对应的处理函数
        │
        ▼
执行 health() → return {"status": "ok", ...}
        │
        ▼
FastAPI 把 dict 序列化成 JSON → 返回给 curl
```

**记忆点**：`uvicorn` 是服务器（管网络），FastAPI 是框架（管路由和处理），两者配合。我们的入口 `app.main:app` 意思是"app 包 main 模块里的 app 对象"。

### 2.3 配置中心（为什么集中读 .env）

**问题**：如果每个文件都直接读 `os.environ.get("DEEPSEEK_API_KEY")`，会怎样？
- 配置散落各文件，改一处漏一处
- 没有类型检查，写错名字静默失败
- 换环境（dev/prod）要改代码

**解决**：pydantic-settings 把 `.env` 读进一个 `Settings` 类：

```python
class Settings(BaseSettings):
    llm_provider: str = "deepseek"   # 字段名 = .env 键名（大小写不敏感）
    deepseek_api_key: str = ""
```

- **单一事实来源**：所有配置一个类管
- **类型安全**：`postgres_port: int` 如果 .env 写了个非数字，启动直接报错
- **环境隔离**：dev/prod 各一份 .env，代码零改动
- **lru_cache 单例**：`get_settings()` 只执行一次，避免重复读文件

### 2.4 为什么 .env 不能进 git（安全底线）

```
.gitignore 写了 .env
        │
git add -A 时跳过 .env
        │
git 仓库里永远没有 key
        │
push 到 GitHub 也不会泄漏
```

**如果 key 泄漏**：任何人都能拿你的 key 调 API 花你的钱。这是真实公司项目的底线，面试官必问。

**配套**：`.env.example` 是脱敏模板（key 留空），给别人参考格式，不泄露真实值。

### 2.5 lifespan（生命周期钩子）

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("启动中...")   # 应用启动时执行一次
    yield                      # 这里让出，应用开始服务请求
    logger.info("已关闭")      # 应用关闭时执行一次
```

**类比**：Next.js 里 server 启动时初始化全局单例（数据库连接、Redis client）。

**为什么需要**：数据库连接池、Redis 连接都是重量级资源，不能在每次请求里创建，要在启动时建好、关闭时销毁。D2/D3 会在这里接数据库和 Redis。

---

## 3. 代码逐行解读（当天关键文件）

### 3.1 app/config.py（配置中心）

```python
model_config = SettingsConfigDict(
    env_file=".env",        # 从项目根目录 .env 读取
    env_file_encoding="utf-8",
    extra="ignore",         # .env 里多出来的键不报错（容错）
)
```
> 这三行是"读 .env 的配置"：指定文件、编码、容错策略。

```python
@property
def database_url(self) -> str:
    return (f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}")
```
> `@property` 让方法像属性一样访问：`settings.database_url`。
> 拼好了 SQLAlchemy 的异步连接串，D2 建数据库直接用。

```python
@lru_cache
def get_settings() -> Settings:
    return Settings()
```
> `lru_cache` = 缓存函数结果，只执行一次。整个应用共享同一个 Settings 实例。

```python
settings = get_settings()
```
> 模块级单例：业务代码 `from app.config import settings` 直接用。

### 3.2 app/main.py（应用入口）

```python
app = FastAPI(title="AgentForge API", ...)
```
> 创建应用实例，title 会显示在自动生成的 API 文档里（/docs）。

```python
@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    logger.exception("未处理异常 path=%s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
```
> 全局兜底：任何没捕获的异常 → 记日志 + 返回 JSON 500（不让裸堆栈泄漏给前端）。

```python
app.include_router(health_router, prefix="/api")
```
> 挂载子路由。`prefix="/api"` 意味着 health.py 里的 `/health` 实际访问是 `/api/health`。

```python
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
```
> 直接 `python app/main.py` 也能启动（开发模式，reload 自动热重载）。

### 3.3 app/api/health.py（健康检查）

```python
router = APIRouter(tags=["health"])

@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.1.0", "deps": {"api": "ok"}}
```
> `APIRouter` = 路由分组（可以多个文件各自建 router，再汇总挂载）。
> `deps` 字段是留给以后数据库/Redis 连通性检查的（D2/D4 填充）。

---

## 4. 类比迁移表（新东西 ↔ 你已会的）

| FastAPI / Python 概念 | 你已会的（TS/前端） | 对应关系 |
|---|---|---|
| `@router.get("/health")` 装饰器 | `app.get('/health', handler)` | 注册路由 |
| `async def / await` | `async function / await` | 异步编程 |
| `-> dict` 类型注解 | TS 返回类型 / zod schema | 类型声明 |
| `BaseSettings` 读 .env | `process.env` + zod 校验 | 配置管理 |
| `lifespan` | Next.js server 初始化 | 生命周期钩子 |
| `uv` 管理依赖 | `pnpm` | 包管理器 |
| `uvicorn` 服务器 | `next dev` / vite dev | 开发服务器 |
| 装饰器（@） | 高阶函数 / 中间件 | 函数增强 |

---

## 5. 面试考点

**Q1：FastAPI 和 Flask 有什么区别？为什么选 FastAPI？**
→ FastAPI 原生异步（高并发好）、Pydantic 自动校验、自动生成 OpenAPI 文档、类型安全。Flask 同步为主，生态老但性能差。AI 应用要流式输出 + 并发，FastAPI 是主流选择。

**Q2：配置管理你怎么做的？**
→ 所有配置集中在 `Settings` 类，pydantic-settings 从 .env 读取，类型校验 + 单例缓存。换环境只改 .env 文件，代码零改动。

**Q3：你的 API key 怎么保护的？**
→ 存在 .env，被 .gitignore 排除，git 仓库里没有。.env.example 是脱敏模板。这是安全底线。

**Q4：健康检查是干嘛的？**
→ 让 Docker/负载均衡知道服务活着。K8s 里 liveness probe 定期请求 /health，挂了就重启容器。

---

## 6. 自测题（不看资料能答出即掌握）

1. 画出一次 GET /api/health 请求的完整生命周期（从 curl 到返回）。
2. `prefix="/api"` 的作用是什么？health.py 里写 `/health`，实际 URL 是什么？
3. 为什么配置文件不用 `os.environ.get()` 散落各处，而要集中到 Settings 类？
4. `lru_cache` 在 get_settings 上有什么用？
5. `.env` 和 `.env.example` 的区别和各自作用？
6. lifespan 里 yield 前后的代码分别什么时候执行？

---

## 7. 踩坑记录

| 坑 | 现象 | 解决 |
|---|---|---|
| Docker daemon 未启动 | `docker info` 报 Cannot connect | `open -a Docker` 启动 Desktop，轮询等待就绪 |
| `.env` 可能被提交 | key 泄漏风险 | `.gitignore` 明确排除，`git ls-files` 验证跟踪数为 0 |
| D1 跳过学原理环节 | 流程不符合约定 | 补复盘课 + 把"每日教程"写入 skill 铁律 8 防再犯 |

---

*D1 完 ｜ 下一篇：D2 SQLAlchemy ORM 与建表*
