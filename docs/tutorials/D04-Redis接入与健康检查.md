# D4 复习教程：Redis 接入与健康检查扩展

> 日期：2026-08-30 ｜ 项目：AgentForge 阶段1 ｜ 状态：已验收通过

---

## 1. 今日目标

学完这一天，你应该能独立回答：

1. Redis 和 PostgreSQL 的区别（快慢/持久性/各自管什么数据）？
2. `ex=3600` 是什么意思？为什么缓存要用 TTL？
3. `decode_responses=True` 是干嘛的？
4. "懒连接"是什么？为什么 `Redis(...)` 不立即连接？
5. 健康检查为什么要"优雅降级"（degraded）而不是直接报错崩溃？

---

## 2. 核心概念讲解

### 2.1 Redis vs PostgreSQL（今天确认题）

| | Redis | PostgreSQL |
|---|---|---|
| 存储 | **内存**（RAM） | **磁盘** |
| 速度 | 微秒级（快 ~100 倍） | 毫秒级 |
| 持久性 | 重启丢数据 | 长期保存 |
| 用途 | **热数据**（高频读写） | **冷数据**（全量/持久） |

**项目用法**：最近 8 轮对话（热）→ Redis；全量历史（冷）→ PG。**热内存 + 冷磁盘**是真实公司缓存架构的标准。

### 2.2 TTL（过期时间）

```python
await redis.set("session:123", "hello", ex=3600)
#                                        ↑ 3600 秒后自动删除
```

- **为什么缓存要过期**：数据会变旧（比如 10 分钟前的对话窗口可能已失效），过期自动清理，不用手动删
- **类比**：localStorage 不会自动清理 → Redis 的 TTL 帮你"到期自动删"
- 防止内存无限增长（过期键 Redis 会自动回收）

### 2.3 decode_responses=True

```python
redis_client = Redis(host=..., port=..., decode_responses=True)
```
- Redis 存的是 bytes（如 `b"hello"`）
- 默认读取返回 bytes，你要手动 `value.decode()` 转 str
- `decode_responses=True`：自动解码成 str，省去手动转换
- **类比**：axios 的 `responseType` 自动解析 JSON，不用每次 `JSON.parse`

### 2.4 懒连接（Lazy Connection）

```python
redis_client = Redis(...)   # 创建客户端对象 —— 此时还没连！
await redis_client.ping()   # 第一次 await 操作 —— 才真正建立连接
```
- `Redis(...)` 只是**创建对象**，不发起网络连接（类比 `axios.create()`）
- **第一次 `await` 操作时才连接**（类比 `axios.get()` 才发请求）
- 好处：导入模块不阻塞，连接按需建立

### 2.5 优雅降级（Graceful Degradation）

```python
if await redis_ping():
    logger.info("Redis 连接正常")
else:
    logger.warning("Redis 连接失败...")   # 只告警，不崩溃！
```

- Redis 是**缓存**，挂了不能拖垮主业务
- 所以连不上 → 记日志 + health 标 `degraded`，应用继续跑
- **类比**：前端某个 CDN 挂了，页面还能显示（只是没图片），而不是整个白屏
- 面试考点：真实服务必须能优雅降级，不能因为非关键依赖崩溃

---

## 3. 代码逐行解读

### 3.1 app/core/redis.py

```python
redis_client: Redis = Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    decode_responses=True,   # bytes → str 自动解码
)
```
> 单例连接对象，配置来自 .env（config 统一管理）。懒连接，不立即连。

```python
async def ping() -> bool:
    try:
        return bool(await redis_client.ping())
    except Exception:
        return False
```
> 健康探测：能 ping 通返回 True，任何异常返回 False（不抛错，调用方安全）。

### 3.2 main.py lifespan（启动自检）

```python
from app.core.redis import ping as redis_ping
if await redis_ping():
    logger.info("Redis 连接正常")
else:
    logger.warning("Redis 连接失败，请检查 redis 容器是否启动")
```
> 应用启动时自检 Redis。**函数内 import** 避免启动时就加载 Redis 依赖（循环导入防御 + 懒加载）。

### 3.3 health.py（健康检查）

```python
redis_ok = await redis_ping()
deps = {"api": "ok", "redis": "ok" if redis_ok else "down"}
status = "ok" if redis_ok else "degraded"
```
> 实时探测 Redis 连通性。任一关键依赖挂了 → 整体 `degraded`（部分可用），不是 `error`。

---

## 4. 类比迁移表

| Redis/Python 概念 | 你已会的（前端） |
|---|---|
| Redis 内存缓存 | 全局内存 state（快但重启丢） |
| TTL 过期 | localStorage 手动清理 → 自动版 |
| decode_responses | axios 自动 JSON 解析 |
| 懒连接 | axios.create() 不发请求 |
| 优雅降级 | CDN 挂了页面仍可用 |
| `redis:123` key 命名 | `session:123` 用冒号分层 |

---

## 5. 面试考点

**Q1：Redis 和 MySQL/PostgreSQL 怎么选？**
→ 互补不互斥：Redis 管热数据（快、易失），PG 管持久数据。典型：缓存/会话窗口/排行榜用 Redis，业务主数据用 PG。Redis 挂了业务降级不崩溃。

**Q2：缓存为什么要设过期时间？**
→ 数据会变旧；不设过期内存无限增长；Redis 自动回收过期键。策略：读多写少 + 容忍短暂过期数据 → 缓存 + TTL。

**Q3：Redis 数据丢了怎么办？**
→ Redis 是缓存不是主存储，丢了可以从 PG 重建（缓存击穿/重建策略）。所以"Redis 只存可重建的数据"是设计原则。

**Q4：health check 为什么标 degraded 而不是 error？**
→ 部分依赖挂了服务还能用（降级运行），degraded 让运维知道"要关注但别杀容器"；全挂才是 error。

---

## 6. 自测题（不看资料能答出即掌握）

1. Redis 和 PG 各存什么数据？为什么这样分？
2. `ex=3600` 在 `redis.set` 里是什么意思？
3. `decode_responses=True` 不设的话，读取返回什么类型？
4. 懒连接：`Redis(...)` 和第一次 `await` 操作，哪个才真正连上？
5. Redis 挂了，我们的应用会崩溃吗？为什么？

---

## 7. 踩坑记录

| 坑 | 现象 | 解决 |
|---|---|---|
| Redis 容器没启动 | health 显示 redis down | `docker compose up -d redis` |
| bytes 乱码 | 读取返回 b'hello' | `decode_responses=True` |
| 连接阻塞 | Redis 挂了启动卡住 | ping 包 try/except + 告警不崩溃 |

---

*D4 完 ｜ 下一篇：D5 Langfuse 接入（学可观测概念）*
