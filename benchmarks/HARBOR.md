# Praxis with Harbor

The external agent is `benchmarks.harbor_agent:PraxisAgent`. Harbor owns task
containers, timeouts, artifacts and grading. Praxis uses its real LLM runtime
with only three container tools; memory, skills, MCP and subagents are disabled.
Original rewards are not converted to binary scores.
Reasoning effort defaults to `max`; context and output token limits are unchanged
from the selected Praxis model configuration.

Set the benchmark-only reasoning effort in `~/.praxis/config.toml`:

```toml
[benchmark]
reasoning_effort = "max"
```

Allowed values: `low`, `medium`, `high`, `xhigh`, `max`. Omission defaults to
`max`. Settings are read during trial setup, not during an active run. With
`--ak config_path=...`, this section is read from that file instead.

## Install

From the repository root in PowerShell (Harbor requires Python 3.12+):

```powershell
conda activate dev
uv tool install harbor==0.23.0 --python 3.12 --with-editable ./backend
$env:PYTHONPATH = (Get-Location).Path
```

The editable backend must be installed in the same Python environment as Harbor.
This does not add Harbor to the production backend or change the project's uv lock.

## Run one task

```powershell
$env:PYTHONPATH = (Get-Location).Path
harbor run -t terminal-bench/photonic-waveguide-routing@4 -a benchmarks.harbor_agent:PraxisAgent -n 1 --max-retries 0 --jobs-dir benchmarks/output/harbor
```

Add `--dry-run` to check configuration without running a model or downloading
the task. A real run uses the active provider in the local Praxis settings and
can incur model charges. Credentials stay in memory; do not pass API keys as
Harbor CLI arguments or enable public result upload.

Optional agent arguments:

- `--ak config_path=C:/path/to/model.toml`: use an explicit Praxis model config.
- `--ak max_tool_calls=32`: opt into a smaller tool budget. Default: no override.
- `--model`, if supplied, must match the selected Praxis model exactly.

Each trial creates isolated Praxis state under its agent logs directory. The
native session database is updated throughout execution; `praxis-trace.jsonl`
is exported before application cleanup, including on errors. Token counts are
reported through Harbor's AgentContext. The context metadata records the actual
model and Git revision (with a dirty marker for uncommitted source).

Cancellation signals the synchronous runtime and joins it before returning to
Harbor. In-flight tool requests are cancelled and new tool requests rejected.
The adapter never creates or deletes the Harbor task container itself.

## Focused tests

```powershell
& "$env:APPDATA/uv/tools/harbor/Scripts/python.exe" -m unittest discover -s tests -p test_harbor_agent.py -v
```

These use simulated environments and do not call a paid model. The ordinary
project test suite skips this module when the optional Harbor package is absent.
Passing these tests is not a claim that a real benchmark task passed.
