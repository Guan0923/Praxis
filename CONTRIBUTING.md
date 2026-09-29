# Contributing to Praxis / 贡献指南

欢迎改进 Praxis 的代码、文档和使用体验。Issue 与 PR 可以使用中文或英文。参与项目交流时，请遵守[行为准则](CODE_OF_CONDUCT.md)。

Issues and pull requests are welcome in Chinese or English. This guide describes the local setup, contribution boundaries, and checks expected before review. See the [English README](README.md) for a product overview and quick start.

## 提交之前

- 先搜索已有 Issue 和 PR，避免重复报告。问题反馈请使用 Bug report 模板，包含最小复现步骤、预期结果、实际结果及运行环境。
- 较大的功能或架构调整，先用 Feature request 模板说明要解决的问题和使用场景，再开始实现。小型修复与文档改进可以直接提交 PR。
- 不要公开 API Key、Cookie、认证头、完整环境变量、私人对话、数据库或私人文件。日志和截图也需要脱敏。敏感问题请参阅[私下联系说明](CODE_OF_CONDUCT.md#reporting)。

## 准备开发环境

本地应用开发面向 Windows，不代表其他系统具备相同的 Windows Sandbox Broker 支持。准备 Python 3.11+、Node.js 22 和 uv。使用 Conda 时先激活 `dev` 环境。

Fork 仓库并克隆自己的副本，然后从仓库根目录执行：

```powershell
git switch -c docs/describe-your-change
conda activate dev
uv sync --locked --dev
cd frontend
npm ci
cd ..
uv run python -m backend.api
```

没有使用 Conda 时，跳过激活命令。分支名称按实际修改内容命名。另开一个终端，从仓库根目录启动前端：

```powershell
cd frontend
npm run dev
```

访问 <http://127.0.0.1:5173>。模型连接和沙箱在设置页配置；安装沙箱需要管理员确认。环境、本地数据与故障排查详见[开发指南](docs/development.md)。

## 修改范围与代码约定

- 每个 PR 聚焦一个问题，先找原因，再做必要修改。保留已有修改和未跟踪文件，不混入无关重构或批量格式化。
- Python 使用四空格缩进、类型标注、`snake_case` 和 `PascalCase`。前端遵循现有 React 组件和 API 层模式。
- Runtime 发布事件，前端负责展示；provider 不依赖 storage；HTTP/SSE 请求使用已有 transport 层。
- 保留工具审批、工作区路径边界、输出上限和沙箱约束。Broker 未就绪时不得静默回退到普通进程。
- Praxis 是本机单用户应用。不要引入账户、登录、云同步或旧安装数据迁移。
- 行为、配置或启动方式变化时，同步更新对应文档；涉及产品介绍时保持两份 README 一致。

详细约束见 [AGENTS.md](AGENTS.md) 和[架构说明](docs/architecture.md)。

## 验证修改

从仓库根目录运行与改动相关的检查。提交业务代码前，除自动化检查外，还要做真实本地验证，并记录操作与结果。HTTP/provider 测试使用 mock 或本地假服务，不调用付费模型 API。

```powershell
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest -q
cd frontend
npm run typecheck
npm test -- --run
npm run build
```

MCP v1 集成测试需要额外准备安装了 `mcp==1.29.1` 的独立 Python 环境，并将 `PRAXIS_MCP_V1_PYTHON` 指向该环境的 Python 可执行文件。

纯文档修改可以核对链接、命令及描述与当前源码的一致性，不必为此运行完整业务测试。Windows pytest 临时目录出现 ACL 错误时，使用当前用户可写且此前不存在的唯一 `--basetemp` 路径，不删除其他任务的目录。

如有检查未运行、失败或跳过，在 PR 中写明原因；不要将局部检查描述成完整验收。

## 提交 Pull Request

1. 检查 `git status --short` 和差异，只提交本次相关文件，不包含密钥、本地数据、日志或生成产物。
2. 用简短标题描述解决的问题，按 PR 模板说明改动前后的行为、关联 Issue 和验证结果。
3. UI 变化附脱敏截图；用户可见的行为变化说明影响及必要操作。
4. 提交到上游仓库的默认分支，按评审意见更新同一个 PR。新增修改后重跑受影响的检查。

评审会关注问题是否解决、范围是否适当、安全边界是否保留，以及验证是否足以支持结论。没有运行的步骤请直接注明。

## 贡献许可

提交贡献时，请确认你有权提供相关代码、文档或其他内容，并同意将贡献按本项目的 [MIT 协议](LICENSE)发布。引入第三方内容时，保留其许可证和必要的版权声明，在 PR 中说明来源。
