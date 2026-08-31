# D2 复习教程：SQLAlchemy ORM 与建表

> 日期：2026-08-29 ｜ 项目：AgentForge 阶段1 ｜ 状态：已验收通过

---

## 1. 今日目标

学完这一天，你应该能独立回答：

1. ORM 是什么？解决什么问题？（对比手写 SQL 的 3 个好处）
2. engine / sessionmaker / Session / Base 分别是干什么的？
3. 外键和级联删除（CASCADE）是什么？为什么 messages 需要它？
4. `server_default` 和 `default` 的区别？
5. FastAPI 依赖注入（Depends）是怎么工作的？

---

## 2. 核心概念讲解

### 2.1 ORM（对象关系映射）

**一句话**：让你用 Python 类操作数据库表，而不是手写 SQL。

| 数据库概念 | ORM 概念 | 你已会的类比 |
|---|---|---|
| 表（table） | Python 类 | Drizzle `pgTable()` |
| 行（row） | 类的实例 | 一条数据 |
| 列（column） | 类属性 | 字段定义 |
| 增删改查 | 方法调用 | Drizzle query API |

**3 个好处**（面试必答）：
1. **不拼 SQL 字符串** → 天然防 SQL 注入
2. **类型安全** → 写错字段 IDE/运行时报错，而不是等到查询失败
3. **换数据库不改业务代码** → 只改连接串（PostgreSQL → MySQL 只需换驱动）

**没有 ORM**：`cursor.execute("INSERT INTO sessions (title) VALUES (%s)", (title,))` —— 字符串拼接，容易出错
**用 ORM**：`sess = Session(title="新会话"); db.add(sess); await db.commit()`

### 2.2 SQLAlchemy async 四件套

```
engine（发动机）→ 真正连数据库，管理连接池（复用连接）
sessionmaker（工厂）→ 生产 Session 的"模具"
Session（工作区）→ 一次事务的操作台（增删改查都在这）
Base（模型基类）→ 所有表类继承它，SQLAlchemy 才知道"这是表定义"
```

**端到端流程**：
```
请求进来 → FastAPI 依赖注入拿一个 Session → add/query/commit
→ commit 时 ORM 把 Python 操作翻译成 SQL 发给 engine → engine 执行
→ 返回结果 → 关闭 Session（归还连接）
```

**类比**：engine = axios 实例（复用连接），Session = 一次 API 调用的 request context。

### 2.3 外键 + 级联删除

- **外键（ForeignKey）**：`messages.session_id → sessions.id`，表示"这条消息属于哪个会话"
- **级联删除（ON DELETE CASCADE）**：删掉一个会话 → 它的所有消息自动删除

**为什么必须级联**：没有它，删会话后消息变"孤儿数据"（指向不存在的会话）。数据库层面保证一致性，比应用代码手动删可靠（应用崩溃时数据库也能保证）。

### 2.4 server_default vs default

| | default | server_default |
|---|---|---|
| 谁生成 | 应用侧（Python 调用函数） | 数据库侧（INSERT 时 DB 自己填） |
| 例子 | `default=_uuid` | `server_default=func.now()` |
| 为什么 | UUID 由应用生成 | 多实例部署时时间统一由 DB 生成，避免各服务器时钟不一致 |

### 2.5 FastAPI 依赖注入（Depends）

```python
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session

# 接口参数里声明依赖：
async def get_session_list(db: AsyncSession = Depends(get_session)):
    ...
```

**流程**：请求进来 → FastAPI 发现参数需要 `get_session` → 自动调用拿到 Session → 传给接口 → 请求结束 → `async with` 自动关闭 Session。

**好处**：连接的生命周期框架帮你管，接口代码只关心业务。类比 React hook 自动管理状态生命周期。

---

## 3. 代码逐行解读

### 3.1 app/core/db.py（连接层）

```python
engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
```
> 创建 engine。`pool_pre_ping=True`：取连接前先 ping 一下，防止拿到已断开的连接（数据库重启后必备）。

```python
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
```
> Session 工厂。`expire_on_commit=False`：commit 后对象属性还能读（否则提交后访问属性会再查库）。

```python
class Base(DeclarativeBase):
    pass
```
> 模型基类。所有模型继承它。

```python
async def get_session():
    async with async_session_factory() as session:
        yield session
```
> 依赖注入：请求进来自动拿 Session，结束自动关闭。`async with` 保证无论成功失败都关闭（防连接泄漏）。

### 3.2 app/models/session.py

```python
id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
```
> UUID 主键。`default=_uuid` 用函数生成，保证每条记录独立 UUID。

```python
summary: Mapped[str | None] = mapped_column(String, nullable=True)
```
> `str | None` = 可空列（允许 NULL）。类型注解直接表达数据库约束。

```python
created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```
> 时间戳：数据库侧默认当前时间（server_default），timezone=True 存带时区的时间。

```python
updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
```
> `onupdate`：每次更新行时自动刷新（ORM 层生效）。

### 3.3 app/models/message.py

```python
__table_args__ = (Index("idx_messages_session", "session_id", "created_at"),)
```
> 复合索引：按 (session_id, created_at) 查消息列表时走索引，不扫全表。

```python
session_id: Mapped[object] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"))
```
> 外键 + 级联删除。`"sessions.id"` 是"表名.列名"。

```python
tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
```
> JSON 列存工具调用记录。非工具消息为 NULL。

### 3.4 scripts/init_db.py（建表）

```python
async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)
```
> `create_all`：遍历所有继承 Base 的模型建表。幂等（已存在的表跳过）。
> 开发期用它；生产用 Alembic（能记录/回滚表结构变更），阶段3 引入。

---

## 4. 类比迁移表

| SQLAlchemy 概念 | 你已会的（Drizzle/前端） |
|---|---|
| `class X(Base)` + `__tablename__` | `pgTable('x', {...})` |
| `Mapped[str]` / `Mapped[int]` | 字段类型 |
| `mapped_column(primary_key=True)` | `primaryKey()` |
| `ForeignKey("sessions.id")` | `references(() => sessions.id)` |
| `Index("name", ...)` | 索引定义 |
| `JSON` 列 | jsonb 字段 |
| `server_default=func.now()` | `defaultNow()` |
| `Depends(get_session)` | React hook / 中间件管理生命周期 |
| `async with ... as session` | try/finally 释放资源 |

---

## 5. 面试考点

**Q1：ORM 和手写 SQL 怎么选？**
→ ORM 用于常规增删改查（类型安全、防注入、可维护）；复杂聚合查询/性能敏感场景手写 SQL 或 SQLAlchemy Core。真实项目两者混用。

**Q2：为什么用 async 数据库驱动？**
→ FastAPI 全异步，如果数据库操作是同步的会阻塞事件循环，并发能力骤降。asyncpg 是 PostgreSQL 最快的异步驱动。

**Q3：外键为什么不放在应用层校验？**
→ 数据库约束是最底层的保证，多实例/多进程下应用层校验有竞态。CASCADE 保证数据一致性由 DB 兜底。

**Q4：建表用 create_all 还是迁移工具？**
→ 开发期 create_all 够用；生产必须用 Alembic 管理表结构版本（能迁移、能回滚、能记录变更历史），团队协作必须有。

---

## 6. 自测题（附参考答案，先自己做再看答案）

1. ORM 对比手写 SQL 的 3 个好处？
   **参考答案**：① 不用手写 SQL 字符串（防 SQL 注入）② 类型安全（IDE/检查器发现类型错误）③ 切换数据库不用改业务代码（只换驱动）。补充：代码可维护性（对象操作 vs 字符串拼接）。

2. engine 和 Session 的关系？（谁管连接池，谁是一次操作）
   **参考答案**：**engine 管连接池**（真正连数据库、复用连接）；**Session 是一次事务的工作区**（增删改查都在这）。Session 从 engine 的池子里拿连接，用完归还。类比：engine = axios 实例（连接复用），Session = 一次 API 请求的 context。

3. `ondelete="CASCADE"` 是干嘛的？没有它会怎样？
   **参考答案**：级联删除——删掉父记录（sessions）时，数据库自动删除所有关联子记录（messages）。没有它：删会话后消息变"孤儿数据"（指向不存在的会话），需要应用层手动清理，且应用崩溃时无法保证一致性。

4. `server_default` 和 `default` 的区别？为什么时间戳用 server_default？
   **参考答案**：`default` 是**应用侧**生成（Python 调用函数）；`server_default` 是**数据库侧**默认（INSERT 时 DB 自己填）。时间戳用 server_default：多实例部署时时间由数据库统一生成，避免各应用服务器时钟不一致；且不依赖应用代码。

5. FastAPI 的 `Depends(get_session)` 里 `async with` 保证了什么？
   **参考答案**：`async with async_session_factory() as session:` 保证 **Session 无论成功/异常都会被自动关闭**（连接归还连接池），防止连接泄漏。FastAPI 自动：请求进来 → 调 get_session 拿到 session → 传给接口 → 请求结束 → 执行 yield 之后的关闭逻辑。类比：try/finally 释放资源，但由框架自动管理。

---

## 7. 踩坑记录

| 坑 | 现象 | 解决 |
|---|---|---|
| Docker 数据库未启动 | 连不上 5432 | `docker compose up -d db` + `pg_isready` 轮询等待 |
| 模型没被建表 | 表缺失 | `models/__init__.py` 必须 import 所有模型（否则 Base.metadata 里没有） |
| 连接池拿到死连接 | 数据库重启后偶发报错 | `pool_pre_ping=True` 取连接前先 ping |

---

*D2 完 ｜ 下一篇：D3 Redis 接入 + 日志完善*
