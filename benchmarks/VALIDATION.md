# 公开 30 题验收记录

日期：2026-09-09。分支：`codex/benchmark-run-status`。环境：Windows + Linux Docker，单容器不超过 4GB，Agent 与评分阶段断网。

这份记录证明题目环境和接入正确，不是 Mini-Agent 的能力成绩。没有调用付费模型 API，也没有启动真实模型的完整 30 题评测。

## 原版评分验收

30/30 题满足：未完成状态不通过，官方参考答案或补丁通过。完整题号、来源版本、镜像 ID、资源和时限见 `public_suite.json`；原始评分记录在独立缓存的 `prepared/` 目录。

| 类别 | 题目 | 未完成状态 | 官方答案 |
| --- | --- | --- | --- |
| 终端 | `tb2-schemelike-metacircular-eval` | 不通过 | 通过 |
| 终端 | `tb2-cancel-async-tasks` | 不通过 | 通过 |
| 终端 | `tb2-custom-memory-heap-crash` | 不通过 | 通过 |
| 终端 | `tb2-git-leak-recovery` | 不通过 | 通过 |
| 终端 | `tb2-sanitize-git-repo` | 不通过 | 通过 |
| 终端 | `tb2-polyglot-c-py` | 不通过 | 通过 |
| 终端 | `tb2-polyglot-rust-c` | 不通过 | 通过 |
| 终端 | `tb2-write-compressor` | 不通过 | 通过 |
| 终端 | `tb2-headless-terminal` | 不通过 | 通过 |
| 终端 | `tb2-circuit-fibsqrt` | 不通过 | 通过 |
| 数据处理 | `tb2-db-wal-recovery` | 不通过 | 通过 |
| 数据处理 | `tb2-extract-elf` | 不通过 | 通过 |
| 数据处理 | `tb2-financial-document-processor` | 不通过 | 通过 |
| 数据处理 | `tb2-large-scale-text-editing` | 不通过 | 通过 |
| 数据处理 | `tb2-log-summary-date-ranges` | 不通过 | 通过 |
| 数据处理 | `tb2-multi-source-data-merger` | 不通过 | 通过 |
| 数据处理 | `tb2-constraints-scheduling` | 不通过 | 通过 |
| 数据处理 | `tb2-regex-log` | 不通过 | 通过 |
| 数据处理 | `tb2-sqlite-db-truncate` | 不通过 | 通过 |
| 数据处理 | `tb2-sparql-university` | 不通过 | 通过 |
| 仓库修复 | `swepro-ansible__ansible-a26c325bd8f6e2822d9d7e62f77a424c1db4fbf6` | 不通过 | 通过 |
| 仓库修复 | `swepro-ansible__ansible-b748edea457a4576847a10275678127895d2f02f` | 不通过 | 通过 |
| 仓库修复 | `swepro-ansible__ansible-d58e69c82d7edd0583dd8e78d76b075c33c3151e` | 不通过 | 通过 |
| 仓库修复 | `swepro-ansible__ansible-1c06c46cc14324df35ac4f39a45fb3ccd602195d` | 不通过 | 通过 |
| 仓库修复 | `swepro-ansible__ansible-e40889e7112ae00a21a2c74312b330e67a766cc0` | 不通过 | 通过 |
| 仓库修复 | `swepro-qutebrowser__qutebrowser-c580ebf0801e5a3ecabc54f327498bb753c6d5f2` | 不通过 | 通过 |
| 仓库修复 | `swepro-qutebrowser__qutebrowser-0fc6d1109d041c69a68a896db87cf1b8c194cef7` | 不通过 | 通过 |
| 仓库修复 | `swepro-qutebrowser__qutebrowser-fd6790fe8c02b144ab2464f1fc8ab3d02ce3c476` | 不通过 | 通过 |
| 仓库修复 | `swepro-qutebrowser__qutebrowser-44e64199ed38003253f0296badd4a447645067b6` | 不通过 | 通过 |
| 仓库修复 | `swepro-qutebrowser__qutebrowser-ed19d7f58b2664bb310c7cb6b52c5b9a06ea60b2` | 不通过 | 通过 |

## 接入与回归

- 真实容器测试 9 项通过：三类任务的 HTTP、Runtime、容器工具、原版评分；未通过但已完成；容器间文件隔离；停止；超时与环境错误；默认工具预算保持不变。
- 30 项逐题隔离检查通过：无宿主挂载、无 Docker socket、断网、内存不超过 4GB、答案与隐藏评分目录不可提前读取；SWE 任务的未来 Git 历史已移除。
- Runtime 相关回归 119 项通过。Benchmark 状态、排队、冲突、HTTP 和工具预算回归 41 项通过。
- 前端 Benchmark 测试 11 项通过，包含连接异常、停止中、来源分类和未通过评分。类型检查、构建和相关 Ruff 检查通过；构建仍有原有大文件体积提示。
- 浏览器确认单题完成、完整 30 题启动、最多 3 项活动、逐项成绩提前出现、停止单项和整批、切换页面与刷新恢复。测试模型仅执行 `pwd`，因此这些 0 分不是 Agent 能力成绩。
- 桌面和 390px 移动端检查完成，无横向溢出；长题号与标签在移动端分行。

## 环境问题与处理

- `build-cython-ext` 的官方答案需要运行时克隆外部代码，替换为 `schemelike-metacircular-eval`。
- `query-optimize` 的原始 SQL 检查耗时过长，替换为 `constraints-scheduling`；没有修改原题评分。
- Windows 脚本换行：从固定 Git 对象导出评分文件，避免 CRLF。
- 包索引缓存过期：Agent 与评分容器设置 `UV_OFFLINE=1`。
- 财务文档题：提前准备原版 OCR 依赖，分别使用环境原有 uv 与评分脚本指定 uv 的缓存；不提前复制答案或处理输入。
- 无头终端题：提前安装原评分脚本要求的 Vim。
- Windows 长路径：内部工作目录使用短名称，记录与页面保留完整题号。

## 边界

这是固定的公开子集，不等同任何官方榜单。原服务未重启，已有文件和未跟踪目录保留；未提交、未推送。来源许可与准备方法见 `THIRD_PARTY_BENCHMARKS.md` 和 `README.md`。
