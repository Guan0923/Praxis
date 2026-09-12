# Praxis Backend

`praxis-backend` 是 Praxis 的 Python 3.11+ 本地服务包，提供 loopback FastAPI、Agent Runtime、模型 Provider、工具、Skills、MCP、Sandbox、进程内 mailbox 以及 TOML/SQLite 持久化。

它面向单机单用户运行，不包含账户、登录、Cloud、同步或 PostgreSQL 服务。

## 安装与启动

backend 是根 `uv` workspace 的成员。请从仓库根目录运行：

```powershell
conda activate dev
uv sync
uv run python -m backend.api
```

服务监听 `127.0.0.1:8000`：

- `GET /api/health`：进程健康检查
- `GET /api/ready`：本地设置与项目数据库就绪检查
- `/docs`：FastAPI OpenAPI 界面

如果 `frontend/dist` 存在，backend 会在 `/` 托管该构建；可通过 `PRAXIS_FRONTEND_DIST` 指定其他构建目录。

消息和临时状态只属于当前后端进程，重启后丢弃；正式聊天历史保存在 SQLite。只运行一份后端，可连接多个浏览器页面。

## 包结构

源码使用 flat layout：`backend/src` 本身映射为 Python 包 `backend`。

```text
src/
├─ api/            FastAPI 装配、Origin 防护、HTTP/SSE 路由
├─ domain/         无外层依赖的消息、计划、会话、Skill 与运行状态
├─ jobs/           本地后台 Job 注册和生命周期
├─ mcp/            审批型外部 MCP 客户端（stdio / SSE / Streamable HTTP）
├─ observability/  日志、指标与递归脱敏
├─ planning/       Planner、上下文与模型请求生命周期
├─ providers/      通用 transport 和 Provider 适配
├─ runtime/        Agent 装配、会话、执行、Plan mode、恢复与事件
├─ sandbox/        Sandbox Launcher、Broker 客户端与本地服务
├─ skills/         Skill 发现、激活与信任
├─ storage/        进程内消息队列、本地 TOML、SQLite 与凭据加密
└─ tools/          ToolRegistry、schema、审批和工具实现
```

依赖方向保持向内：`domain` 不依赖外层，Provider 不导入 storage，Runtime 发布事件而不包含前端展示逻辑。Sandbox Broker 不可用时不得静默降级。

## API 分组

| 路径 | 用途 |
| --- | --- |
| `/api/settings` | Profile、Agent、Runtime、Sandbox 与 Provider 设置 |
| `/api/projects` | 本地项目、会话与项目 Skill 信任 |
| `/api/sidebar-threads` | 对话列表、归档、恢复与删除 |
| `/api/sidebar-threads/{id}/queued-messages` | 内存队列 待发送消息 CRUD |
| `/api/turns` | Turn 创建、SSE、暂停、恢复、steering、rewind、fork 与 compact |
| `/api/sessions/{id}/files` | 会话文件上传、列表、读取与删除 |
| `/api/jobs` | 后台 Job 查询与取消 |
| `/api/sandbox` | Sandbox 状态、安装与修复 |
| `/benchmark` | 独立挂载的本地 benchmark 子应用 |

浏览器非只读请求会校验 `Origin`。默认只允许配置的 loopback 来源；`PRAXIS_ALLOWED_ORIGINS` 接受逗号分隔的精确 Origin。无 `Origin` 的本地 CLI 请求允许执行，CORS 不启用 credentials。

## 配置与持久化

默认数据根目录为 `~/.praxis`：

```text
~/.praxis/
├─ mcp/
├─ plugins/
├─ runtime/
│  ├─ state.db
│  ├─ projects.db
│  └─ <session_id>/
├─ skills/
└─ config.toml
```

`config.toml` 只保存非敏感配置。Provider API Key 使用 OS credential vault 中的安装级密钥加密后写入 `runtime/state.db`。不要把密钥、Cookie、认证头或完整环境写入日志和测试输出。


## MCP 客户端

Praxis 只作为 MCP 客户端使用外部工具、资源与提示词，不提供 MCP 服务端命令。设置页直接编辑整份 JSON，保存会替换全部服务器配置；删除条目即删除服务器及其托管凭据。

```json
{
  "mcpServers": {
    "local": {
      "type": "stdio",
      "command": "node",
      "args": ["/absolute/path/server.js"],
      "env": {"TOKEN": "your-token"},
      "timeout": 30,
      "disabled": false
    },
    "remote": {
      "type": "streamableHttp",
      "url": "https://example.com/mcp?apiKey=your-key",
      "headers": {"Authorization": "your-original-authorization-value"}
    },
    "events": {
      "type": "sse",
      "url": "http://127.0.0.1:8080/sse"
    }
  }
}
```

- 必填 `type`：`stdio`、`sse`、`streamableHttp`。
- `stdio` 必填 `command`；`args` 默认 `[]`，`env` 默认 `{}`，`cwd` 默认用户家目录。相对脚本路径相对于此目录。
- 两种 HTTP 连接必填 `url`，可选 `headers`（默认 `{}`）。URL 查询参数保持原样，请求头值不添加 Bearer 前缀或做其他编码。
- `timeout` 是每台服务器初始化和单次调用的超时秒数，默认 30，必须是有限正数。
- `disabled` 默认 false。总开关和配置修改影响下一次运行，不修改正在运行的连接。
- 测试使用已保存配置，忽略总开关和该服务器的 disabled，测试完成后关闭连接。有未保存修改时需先保存。
- 不支持 `requestInit`。协议管理的请求头（例如 Host、Content-Length、MCP-Protocol-Version）不能由配置覆盖。

敏感环境变量、认证请求头和 URL 密钥存入 OS 凭据库，TOML 配置只保存引用。编辑器回读以 `<stored-secret>` 标记已保存的密钥：原样保留即沿用，输入新值即替换，删除字段即删除。修改含占位符的 URL 时须重新粘贴完整 URL；不同服务器不能复用占位符。普通请求头名称按 HTTP 不区分大小写的规则统一保存为小写。

HTTP 可明文传输内容；HTTPS 校验证书，连接不自动跟随重定向。不提供 OAuth。错误保留连接失败详情，URL 中的密钥和认证信息不会出现在返回的错误或 HTTP 传输日志中。

资源和提示词通过审批型 Agent 工具按需读取。订阅只在当前运行内有效，更新只记录 URI，Agent 检查后决定是否重读；外部提示词不会覆盖系统消息。

## 验证

从仓库根目录运行：

```powershell
conda activate dev
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest -q
```

HTTP 与 Provider 测试使用 mock 或本地假服务，不调用付费模型 API。
