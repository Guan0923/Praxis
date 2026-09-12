# Prompt 模板

该目录保存运行时系统提示模板。

- `instruction.md`：稳定的基础身份与安全边界。
- `default.md`：Default 模式的完整指令，对应运行配置中的 `agent`。
- `plan.md`：Plan 模式的完整指令。
- `title.md`：首消息标题生成约束。
- `__init__.py`：分别加载基础系统指令和带 `<collaboration_mode>` 标签的模式指令。

Turn 开始时保存一次 `developer` 模式消息；运行中切换在当前 Item 结束后追加新消息。历史记录保留 `developer`，模型请求转换成 `user`，模式指令不再拼进 system。
