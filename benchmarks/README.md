# Mini-Agent 公开综合基准

正式清单为 `public_suite.json` 中固定的 30 题。保留原始提示词、完整任务环境与上游评分，不再使用缩减后的代码片段。旧 9 题仅作为流程测试数据保留，不进入正式注册列表。

这不是任何上游排行榜的官方实现，分数不能与上游官方成绩直接比较。

## 任务构成

| 来源 | 数量 | 能力类型 |
| --- | ---: | --- |
| Terminal-Bench 2.0 | 10 | 终端任务 |
| Terminal-Bench 2.0 | 10 | 数据处理 |
| SWE-bench Pro 公开集 | 10 | 完整仓库修复 |

所有正式任务使用 `llm` planner。原题指定执行时限时沿用原值，否则为 60 分钟；默认不限制工具次数。排除 GPU 与外部账号任务，单容器内存不超过 4GB。

## 准备与验收

需要运行中的 Linux Docker。缓存默认在 `~/.cache/mini-agent-benchmark`，可通过 `MINI_AGENT_BENCHMARK_CACHE` 指定独立位置。

```powershell
conda activate dev
uv run --with pyarrow==25.0.1 python -m benchmarks.prepare --sources
uv run python -m benchmarks.prepare --verify
```

`--sources` 下载固定版本源码和数据；`--verify` 逐题运行未修改状态和官方参考答案，不调用模型。也可用 `--task NAME` 指定单题。只有未修改状态不通过、参考答案通过的题，才标为已验收。详细记录位于缓存的 `prepared/`。

本轮 30 题验收与浏览器验证见 [`VALIDATION.md`](VALIDATION.md)。真实容器接入测试使用独立本地模型响应服务：

```powershell
$env:MINI_AGENT_TEST_DOCKER = "1"
uv run python -m pytest -q tests/test_benchmark_containers.py
```

镜像及评分依赖在准备阶段下载。正式 Agent 和评分容器均断开外网，不挂载宿主目录、Docker socket 或模型凭据。Docker 下载失败会报环境错误，不降级为宿主执行，也不自动改动 Docker 配置。

本机 Docker Hub 直连不可用时，本轮使用官方 `crane` 经现有系统代理下载原始镜像归档，再以 `docker load` 导入；没有改动 Docker daemon 或代理配置。归档保留在独立缓存中，后续可重新导入同一镜像。

Windows 下评分脚本直接从固定 Git 对象导出，避免 CRLF 转换。准备依赖时不复制测试或答案；评分在 Agent 退出后的独立容器中进行。SWE 任务移除未来 Git 历史，评分容器另行恢复原版测试。

清单同时固定 Linux/amd64 原始镜像 ID；标签指向其他镜像时拒绝执行。离线容器设置 `UV_OFFLINE=1`，避免依赖缓存过期后尝试访问包索引。财务文档题提前缓存原版声明的 OCR 依赖，不提前运行或复制解题代码。

## 列出与运行

### 浏览器运行

页面启动任务后立即显示运行状态。单项和整批共用 3 个执行名额，其余任务排队；同一任务不能重复启动。每次执行使用独立的配置、MCP、会话和工作目录，模型配置在启动时读取，凭据不写入 benchmark TOML。

页面每秒查询状态，逐项显示成绩。切换页面或刷新不会停止任务；停止单项不影响其他项，停止整批会取消该批排队项，并等待正在执行的任务真正退出。已完成但评分未通过仍显示“已完成”。状态与成绩保留到后端关闭，重启后不自动恢复或重跑。

| 接口 | 返回 |
| --- | --- |
| `POST /benchmark/run`、`POST /benchmark/run-all` | `202`，运行编号和初始状态 |
| `GET /benchmark/runs` | 后端实例编号、运行列表及逐项状态，不含完整 Trace |
| `GET /benchmark/runs/{run_id}` | 单次运行详情 |
| `GET /benchmark/runs/{run_id}/tasks/{task_id}/trace` | 按需读取完整 Trace |
| `POST /benchmark/runs/{run_id}/cancel` | 停止整批 |
| `POST /benchmark/runs/{run_id}/tasks/{task_id}/cancel` | 停止单项 |

浏览器写请求仍使用现有 Origin 和窗口操作协议；重复启动返回 `409`，未知运行编号返回 `404`。

### 命令行运行

以下命令均从仓库根目录运行。列出任务不会调用模型：

```powershell
conda activate dev
uv run python -m benchmarks.run --list
```

运行一个任务、多个任务或完整套件：

```powershell
uv run python -m benchmarks.run --task tb2-cancel-async-tasks --config C:\path\to\benchmark.toml
uv run python -m benchmarks.run --task task-a --task task-b --config C:\path\to\benchmark.toml
uv run python -m benchmarks.run --all --config C:\path\to\benchmark.toml --output report.json
```

`--task` 可重复，也接受逗号分隔的任务名。未提供 `--task` 时默认选择全部任务；还可通过 `--capability terminal|software_engineering|data_processing` 过滤。页面筛选只影响展示，“全部运行”始终执行完整题库。

## 模型配置

`llm` 是默认 planner，需要包含以下字段的 OpenAI-compatible TOML：

```toml
[model]
api_key = "..."
base_url = "http://127.0.0.1:8001/v1"
model = "model-name"
```

网页沿用当前应用模型配置，推荐从网页启动正式评测。CLI 的独立测试配置只在内存解析凭据，不把 API Key 复制到执行目录；报告和日志不得输出凭据。验证命令不需要模型配置。

## 重复、输出与调试

重复每个任务以观察模型波动：

```powershell
uv run python -m benchmarks.run --all --repeat 3 --config C:\path\to\benchmark.toml
```

`--repeat N` 要求 `N >= 1`。每次 attempt 都使用新的 workspace 和 MCP 状态；重复运行会按比例增加模型调用量和费用。报告保留每次 attempt，并按任务通过率等权计算总分。

常用选项：

| 选项 | 用途 |
| --- | --- |
| `--output PATH` | 指定 JSON 报告；默认写入 `benchmarks/output/<timestamp>/report.json` |
| `--sandbox PATH` | 指定 benchmark 客户端状态根目录 |
| `--keep-workspaces` | 保留每个任务的 workspace 以便调试 |
| `--max-tool-calls N` | 覆盖所有所选任务的工具调用预算 |

启动后在后台准备容器、初始化 Runtime、执行和评分。除非使用本地模型端点，否则正式 `llm` 运行会访问配置的模型 API；环境验收不调用模型。

## 评分

Terminal-Bench 保留原始 reward；SWE-bench Pro 根据上游 parser 核对全部指定的 fail-to-pass 与 pass-to-pass 测试。执行失败、环境错误和超时不伪装为有效零分。代码完成但评分不通过，仍属于已结束的运行。

JSON 报告保存逐次结果、任务通过率和 `by_source` 分来源汇总。页面按来源独立显示通过数与已评分数量。来源元数据包含原题编号、固定版本、许可证说明和执行时限。

## 来源与许可

详见 [`THIRD_PARTY_BENCHMARKS.md`](THIRD_PARTY_BENCHMARKS.md)。完整源码、原始数据、答案和评分文件保存在独立缓存，不向 Agent 提前提供 gold patch。
