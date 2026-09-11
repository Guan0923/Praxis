# 本地持久化

该包保存 Session、SidebarThread、Turn、审批、项目和设置。

- `__init__.py`：公开 SQLite 与 进程内消息队列 存储入口。
- `message_queue.py`：稳定导出门面，保持 `MemoryMessageQueue` 和 mailbox 的既有导入路径。
- `message_queue_support.py`：消息合并与重复请求比较。
- `memory_message_queue.py`：`MemoryMessageQueue` 提供同一协议的线程安全内存实现，作为唯一生产实现。
- `message_mailbox.py`：`TurnMailbox`/`AgentMailbox` 将 queue 适配为 Runtime 与 Subagent mailbox port。
- `sqlite.py`：`SQLiteSessionStore` 聚合各 SQLite mixin。
- `sqlite_base.py`、`sqlite_schema.py`：连接/事务基础与 schema。
- `sqlite_json.py`：`read_json_object` 提供 Session 与 Runtime mixin 共用的单对象读取和 JSON object 校验。
- `sqlite_sessions.py`、`sqlite_sidebar_threads.py`、`sqlite_approvals.py`：领域表操作。
- `sqlite_runtime/`：Runtime Node、Checkpoint 和 JSON record。
- `projects.py`：`ProjectStore`；`settings/`：TOML/SQLite 设置与凭据加密。
- `codec.py`：RuntimeState/消息 JSON 编解码。

存储层不发布 UI 事件；进程内消息按队列顺序投递，SQLite 幂等持久化成功后才 确认消费。事务边界由 repository 明确控制，secret 不得写入 `config.toml`。

Storage 包装类型只保留队列/事务等控制语义；任何向 HTTP、Runtime、Job 或审计传播的错误文本都必须统一投影并脱敏最底层异常消息。
