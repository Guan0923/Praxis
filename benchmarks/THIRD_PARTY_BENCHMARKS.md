# Third-party benchmark provenance

## Active public-30-2026-09-09 suite

The fixed manifest is `public_suite.json`: 20 Terminal-Bench 2.0 tasks and 10
SWE-bench Pro public instances. Prompts and requirements are imported without
abridging the problems; source repositories, tests and reference solutions stay
in the independent download cache, never in the initial agent workspace.

| Source | Fixed revision | Notice |
| --- | --- | --- |
| harbor-framework/terminal-bench-2 | `2fd12b88aafdd04a52c298e3940bcb189f9766d6` | Apache-2.0; original task canaries retained |
| ScaleAI/SWE-bench_Pro dataset | `7ab5114912baf22bb098818e604c02fe7ad2c11f` | Public dataset card does not separately declare a data license; do not infer one from the harness license |
| scaleapi/SWE-bench_Pro-os harness | `ca10a60a5fcae51e6948ffe1485d4153d421e6c5` | MIT; individual repository licenses still apply |

Only package preparation and orchestration are local additions. Upstream test
launchers and parsers are exported from pinned Git objects, without rewriting
their checks. Source snapshots and prepared image IDs are recorded separately.
Network is available during preparation only. This curated subset and its
container resource limits are not an official leaderboard submission.

Excluded candidate families include GPU training/inference, account-dependent
services, large distributed jobs, and long compilation/compute workloads.
`build-cython-ext` was replaced by `schemelike-metacircular-eval`: its reference
solution clones a repository during execution, conflicting with offline runs.
`query-optimize` was replaced by `constraints-scheduling`: the original baseline
verifier spent more than eight minutes executing its SQL workload without a
result on this machine. The replacement tests parsing multiple ICS datasets,
hard availability constraints and ordered preferences without heavy compute.
Per-task baseline/oracle validation records (including failures) live in the
cache's `prepared` directory; inclusion in the manifest alone does not mean
the task has passed environment acceptance.

## Retained local test fixtures (not registered)

The Praxis suite is an adapted, offline subset. The source projects and
their task contracts remain the authoritative references; this repository does
not claim official leaderboard compatibility.

| Adapted task family | Upstream source | Revision used | License/notice |
| --- | --- | --- | --- |
| Terminal-Bench: `analyze-access-logs`, `cancel-async-tasks`, `countdown-game` | [harbor-framework/terminal-bench](https://github.com/harbor-framework/terminal-bench) | `terminal-bench-core-0.1.1` task contracts | Apache-2.0; Harbor Framework attribution and the upstream task canary reference are retained in each task's source metadata |
| SWE-bench Lite: `psf__requests-2317` | [SWE-bench Lite dataset](https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite) | base commit `091991be0da19de9108dbe5e3752917fea3d7fdc` | Requests Apache-2.0; SWE-bench data/harness MIT |
| SWE-bench Lite: `pytest-dev__pytest-11143` | [SWE-bench Lite dataset](https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite) | base commit `6995257cf470d2143ad1683824962de4071c0eb7` | Pytest MIT; SWE-bench data/harness MIT |
| SWE-bench Lite: `astropy__astropy-14365` | [SWE-bench Lite dataset](https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite) | base commit `7269fa3e33e8d02485a647da91a5a2a60a06af61` | Astropy BSD-3-Clause; SWE-bench data/harness MIT |
| τ³-bench retail tasks 0 and 113 | [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench) | `v1.0.1` | MIT |
| τ³-bench airline task 3 | [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench) | `v1.0.1` | MIT |

The Praxis adaptations intentionally remove Docker wrappers, network calls,
large dependencies, and multi-turn user simulators. The τ³ tasks flatten the
user simulator into a single authorized request while retaining policy,
tool-argument, and final-state requirements. The SWE tasks vendor only the
minimal affected code path and replace upstream test patches with local hidden
regression checks. These transformations are adaptation work, not upstream
gold patches.

Terminal-Bench attribution/canary notice: this suite names the Harbor Framework
source task and pinned task-contract revision in `benchmarks/tasks/open_source.py`.
The local fixtures are deliberately rewritten and therefore must not be used as
official Terminal-Bench canary submissions.
