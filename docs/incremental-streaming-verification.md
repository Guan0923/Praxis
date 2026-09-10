# 模型消息增量保存与显示

日期：2026-09-10。分支：`codex/incremental-turn-streaming`。
基线：main 的 `fc5e8ac8`。没有提交、推送或重启原有服务。

## 数据与运行路径

- SQLite 存储版本为 17。旧库直接拒绝打开，不做迁移、删除或覆盖。
- 创建 Turn 保存基础快照；运行中写 `runtime_delta:<turn_id>`。每条记录保留事件 ID、序号及原始增量。
- `runtime_event_outbox` 是独立的待发送记录。确认发送只删除 outbox，不删除恢复日志。
- `NodeWriter` 为同一份不可变增量分配顺序。主对话和子 Agent 的显示直接发布 Redis，保存交给容量为 256 的有界队列。
- 队列满时生产者等待；没有定时攒批、不丢弃文字。每条增量单独提交 SQLite。
- 后台保存线程复用连接。读取使用一致的只读事务，写入先取得写锁，避免读后升级写锁与事件确认互相阻塞。
- 读取入口统一还原快照和后续增量，包括历史、恢复、回退、Trace 的 Turn 读取及侧栏摘要。
- Item Trace 在相应内容保存后写入。收尾、暂停和版本切换先等增量完成，再原子合并快照、删除已合并日志。
- 终态和 SSE 成功通知在保存完成后发送。保存失败终止生成并显示错误；能写数据库时只封存已保存前缀为失败，不能写时交给下次启动恢复。
- 运行中重连取内存快照和同一时刻的序号。游标带进程代次，重启不复用旧代次游标；已覆盖的旧终态也不重放。

进程突然退出时，已显示但尚未提交 SQLite 的尾部可能丢失。这是显示不等待保存的明确边界；恢复只承认已落盘内容。

## 前端与 Todo

- 保留原有按画面刷新合并更新。普通增量保留其他 Item 的对象引用。
- 继续使用 markdown-it/texmath；根据解析器的语法块和源位置重解析可变尾部，不按空行切分。
- 列表、表格、代码围栏、未闭合公式保留在可变区域；后置链接定义重新处理受影响的旧段落。
- Item 完成允许一次完整校正；DOM 按稳定块位置更新。回答收尾只隐藏动画，不卸载正文。
- 未变化公式保留节点；已移除公式调用 MathJax 清理，过期异步结果不能覆盖新内容。保留 MathML、安全过滤、SVG 回退和原始公式复制。
- Todo 候选正文立即输出。无工具调用时仍检查未完成项，最多增加一次收尾，不撤回候选正文，也不重复补发。
- 保留原工作区已有的回退请求受理和失败恢复修改。

## 本机对比

使用基线源码的独立 Python 环境及构建产物，与本分支分别运行相同的本地 HTTP 模型源。真实 Redis 使用独立前缀，SQLite 使用独立临时数据目录。
输入包含 100 个带编号的中文段落及 Markdown/公式片段；共 116 个模型片段，每片间隔约 10 ms。表格统计其中 100 个可唯一匹配的段落。

| 阶段，中位耗时 | main 基线 | 本分支 |
| --- | ---: | ---: |
| 模型发出到后端处理 | 2453.22 ms | 2.04 ms |
| 后端处理到发布完成 | 47.94 ms | 1.57 ms |
| 模型发出到浏览器收到 | 2488.52 ms | 6.51 ms |
| 浏览器收到到页面变化 | 22.00 ms | 15.00 ms |
| 模型发出到页面变化 | 2511.02 ms | 22.44 ms |
| 单次 SQLite 写入 | 9.22 ms | 5.63 ms |
| 后端处理到保存完成 | 11.17 ms | 20.29 ms |
| 未变化公式节点被替换 | 115 次 | 0 次 |

页面可见延迟 P95：4131.68 ms → 32.20 ms。保存完成延迟 P95：27.86 ms → 53.69 ms，说明后台写入确实可能积压，不能把更快显示理解成所有保存延迟都下降。
这些是本机单组样本，不是所有模型、机器或工作负载的提速保证。页面变化由浏览器 MutationObserver 记录，不等同于屏幕实际发光时间。

原始记录在本 worktree 的 `.test-tmp/pipeline-before-real-01/test_browser_with_real_redis_h0/` 和 `.test-tmp/http-final-03/test_browser_with_real_redis_h0/`，包含 `pipeline-backend.json`、`pipeline-browser.json` 和桌面/窄屏截图。
误用共享可编辑 Python 环境的初次基线样本未用于上表；上述基线已核对导入路径确实来自归档的 main 源码。

## 复验

最终验证结果：

- 相关后端 118 项通过；随后增加一项旧内存视图不可变回归，并连同 Trace/多消息共 33 项复验通过。日志记录格式的最后调整另有 25 项复验通过，重复项不累计。
- 四类真实 HTTP/Redis/SQLite/浏览器测试全部通过：控制流程、富文本连续输出、Todo、写入失败，分两次各运行两项。
- 前端全套 424 项通过；之后补充的两项异步公式过期/清理测试也通过。
- TypeScript 类型检查、前端构建、Ruff 检查、格式检查及 `git diff --check` 通过。
- 检查了 1365×900 桌面及 390×844 窄屏截图。富文本最终内容完整，未变化公式在追加和收尾时均未替换。
- 没有运行所有无关后端测试。前端测试仍有现有 act/jsdom 警告，构建仍提示大包体积，没有将这些警告写成已解决。

```powershell
conda activate dev
uv sync
cd frontend
npm ci
npx playwright install chromium
npm run typecheck
npm test -- --maxWorkers=2 --minWorkers=1
npm run build
cd ..
uv run python -m pytest -q -s tests/test_chat_http_responsiveness.py --basetemp=.test-tmp/unique-http-run
uv run python -m pytest -q tests/test_incremental_runtime.py tests/test_turn_protocol.py tests/test_runtime_event_transport.py tests/test_todo_runtime.py tests/test_turn_trace.py tests/test_turn_multi_message_protocol.py tests/test_sidebar_thread_summaries.py tests/test_subagents.py --basetemp=.test-tmp/unique-runtime-run
uv run python -m ruff check backend tests
uv run python -m ruff format --check backend tests
```

HTTP 测试使用显式测试 Sandbox launcher，不验证系统沙箱隔离，也不访问付费模型 API。Redis 必须可连接。每次运行应使用新的 basetemp 名称。
计时汇总工具：`tests/support/streaming_metrics.py <before-directory> <after-directory>`。

手动运行必须指定一个新的空目录和空闲端口，例如：

```powershell
uv run python -m backend.api --data-root C:\praxis-streaming-new-data --port 8017
```

不要将测试新版本指向旧 `.praxis` 数据目录。该命令没有在原有服务上执行。
