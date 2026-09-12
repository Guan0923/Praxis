# 默认 Tool 定义

该包把 Handler 能力包装为模型可见的 Tool schema。

- `filesystem.py`：只读文件工具和统一的 `file_operation` 写操作入口。
- `command.py`：`run_command` 与 `write_stdin`；`web.py`：search/fetch；`time.py`：当前时间。
- `todo.py`：`update_todo_list`；`schema.py`：`object_schema`。
- `__init__.py`：`build_default_tools` 汇总只读/可写集合。

schema 与 Handler 参数必须同步；审批不能替代具体 Handler 的安全检查。

`file_operation(operation, path, ...)` 支持 `create`、`write`、`delete`：

- `create` 必填 `type=file|directory`；文件可提供初始 `content`，缺失父目录自动创建，已有文件不覆盖。
- `write` 必填 `content`。仅当 `start_line=end_line=-1` 且 `expected_content=""` 时整份覆盖，文件不存在则创建；其他情况核对指定行原文后替换。行号默认 `-1`，原文默认空字符串。
- 原文按所选行的内容用换行连接，不包含最后一行的行尾换行；空 `content` 删除所选行或清空文件，不删除文件本身。
- `delete` 仅接受 `path`，删除文件或整棵目录；不存在目标、工作区根目录、越界和链接路径报错。

所有操作保留权限、审批和工作区限制；错误返回工具结果，不能绕过检查自动重试。
