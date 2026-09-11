# Conversation Cache, History Pages, and Interactive Commands

Date: 2026-09-11
Branch: codex/redis-sandbox-recovery

This change builds on the uncommitted Redis removal and sandbox recovery work.
It does not commit, merge, push, restart existing services, or change the
sandbox installation/repair transaction. Tests use temporary SQLite directories,
loopback ports, and owned child processes. No paid model API is used.

## Cache Ownership

- The sidebar Thread is the cache owner. Independent branch Threads count
  separately; descendant Agent Threads belong to their sidebar root.
- Running Turns (including approval waits), commands owned by those Turns,
  and pending deliveries prevent eviction. An open browser alone does not.
- Explicit history reads and terminal opening update access order. Runtime
  events and SSE heartbeats do not refresh idle access order.
- All active owners remain; only five idle owners remain, shared by all pages.
  Finishing an active owner moves it to the most-recent idle position.
- Eviction releases runtime events, Todo state, terminal replay, idle page
  terminals, and empty delivery indexes. Acknowledged delivery bodies are
  released while minimal receipt identity remains to reject duplicate work.
- SQLite history and Trace records are not deleted. Reopening fetches history,
  not fabricated Todo state or terminal output. No cache TTL is added.
- Browser detail caches retain active conversations plus five idle ones, while
  sidebar summaries remain. Each browser page has its own loaded history range.
- An evicted idle SSE stream ends with thread.evicted and does not recreate
  the discarded cache merely because a browser remains connected.

## History Interface

GET /api/turns/history accepts session_id, thread_id, before, and limit.
limit defaults to 5, with a maximum of 100. The response contains turns,
next_cursor, and has_more. Treat the cursor as opaque.

The store follows parent Turn references with bounded individual SQLite reads,
including inherited branch history. It does not load every Turn before slicing.
Session admission checks read the session document rather than constructing a
full history summary. Formal model context and Trace export remain independent.

The first page contains the most recent five Turns. Scrolling upward prepends
five more, deduplicates by ID, and restores the reading anchor. Recent runtime
updates and recovery refreshes retain already appended history. Stale responses
cannot overwrite a changed head/cursor; invalidated cursors reload a new page.
Subagent paging updates its own history state without replacing the root chat.

## Command Interface

run_command accepts cmd, yield_time_ms=10000, max_output_tokens=2000.
write_stdin accepts session_id, chars="", yield_time_ms=60000,
max_output_tokens=2000. Wait values range from 0 through 300000 milliseconds;
token budgets must be positive integers. The previous run_command parameters
are removed rather than accepted through compatibility aliases.

Both tools return JSON text containing output, status, and output_truncated.
A running result includes session_id; a finished result includes exit_code.
Nonzero exit keeps the tool failure and original error_report. Omission counts
are available as cache_omitted_bytes and/or response_omitted_bytes when relevant.

- Waiting ends early when the command exits; reaching the wait interval does
  not kill it. There is no tool-level 60-second total timeout.
- Existing sandbox wall/resource restrictions remain. The command lease stays
  held through actual process completion and cleanup.
- Empty chars reads new output. Nonempty chars writes stdin under the usual
  tool approval policy. Ctrl+C interrupts the owned command without a new
  approval. The process-tree kill fallback is used where console signaling is
  unavailable.
- Command IDs are bound to the owning Turn and run scope. Reads/writes are
  serialized. Finished output is readable until scope cleanup.
- Turn completion, pause, cancellation, and backend shutdown close the scope,
  terminate the owned process tree, release output, and wake waiting readers.

## Output Limits

Every command has its own combined stdout/stderr 1 MiB cache. Every page
terminal has a separate 1 MiB cache. These are not a process-wide memory limit.
Each keeps up to 512 KiB at the start and 512 KiB at the end. The middle is
removed during reception; no hidden complete command output is retained.
UTF-8 boundaries are preserved, omitted byte counts are explicit, and source
switches between stdout and stderr remain visible.

Each tool response additionally applies the existing token counter and hard
character cap to the output field, including its omission marker. Control
metadata and the original error report are separate JSON fields. A tiny budget
that cannot hold a marker returns empty output with omission-count metadata.
The read cursor advances across omitted content, so later reads do not replay
it. SQLite stores the actual returned tool result, not an extra output dump.

When a terminal replay has a gap, WebSocket output uses ordered segments with
data or omitted_bytes instead of embedding the omission marker in raw data.
This keeps the browser from re-counting a backend marker as discarded output.

Page terminals receive increments, replay a retained head/tail snapshot after
refresh, and bound their browser-side raw output and rendered scrollback.
Disconnect no longer starts a timed terminal cleanup; explicit close, owner
cache eviction, and backend shutdown still clean up. SSE retains its existing
10,000-event-per-stream bound.

## Verification

Completed checks during implementation:

- Real local command launch, early return, input with newline, empty follow-up,
  Ctrl+C, nonzero exit, cross-Turn rejection, scope cleanup, process-tree exit,
  blocked-reader wakeup, and maintenance-lease lifetime.
- Output above 1 MiB, real UTF-8 Chinese output, combined stdout/stderr,
  head/tail byte accounting, token limits, and no replay of discarded output.
- Real SQLite/HTTP history pages, branch ancestry, forbidden full-history read,
  idle eviction, queued/active retention, Todo/event release, and SSE eviction.
- Real Windows PTY input/output and existing terminal WebSocket contract tests.
  Chromium also typed into the actual terminal and verified output replay after
  refresh, waiting for the existing three-second window ownership handoff.
- Chromium with a real loopback backend and seeded SQLite: initial 5 Turns,
  prepend to 10 then 12, preserved scroll position, no request past oldest,
  refresh back to 5, independent second page, and 390px mobile width.
  Desktop/mobile screenshots were inspected; no browser page errors or
  horizontal overflow were found.

Final checks:

- Ruff check: passed. Ruff format check: 481 files already formatted.
- Frontend typecheck: passed; Vitest: 449 passed; production build: passed.
  Vite still reports the existing large-chunk warning.
- Full backend runs were performed. The final acceptance rerun had 1300 passed,
  49 skipped, 2 failed, and 1 explicitly deselected known baseline test.
- The known baseline test is
  test_agent_threads.py::test_legacy_schema_is_rejected_without_mutating_or_deleting_database.
  It expects v16 while the application requires v17; its failure was confirmed
  in the unfiltered full runs and is not counted as passing.
- The two acceptance-run failures were
  test_running_subagent_bridge_accepts_live_runtime_config (an immediate
  persisted-permission assertion) and
  test_browser_with_real_memory_http_and_small_model_chunks[rich-stream]
  (waiting for the editor). Both passed together in an isolated rerun without
  changes to those tests or their production implementations. They are recorded
  as intermittent failures, not a clean full-suite result.
- Focused queue/cache/terminal/process checks: 46 passed. Final command, history,
  and real-browser supplementary checks: 40 passed, including terminal typing
  and replay after refresh.
- git diff --check passed. Existing main-worktree modifications remain intact.

No claim is made that the backend full suite is entirely green. The unrelated
version assertion and intermittent checks remain visible for follow-up.

Privileged production Broker/service behavior was not exercised or restarted.
The real command lease test uses the existing direct test launcher and an
in-process maintenance gate; it is not proof of privileged sandbox isolation.
Docker/download/MCP-v1/POSIX-only tests remain environment-dependent skips.
