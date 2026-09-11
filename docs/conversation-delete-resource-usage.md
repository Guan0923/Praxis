# Conversation deletion and model-command resource accounting

## Scope

Implementation branch: codex/conversation-delete-resource-usage (from main 33dea1de).
No commit, merge, push, installed-service restart, production-data changes, or paid model calls.
Web-search timeouts and sandbox automatic recovery are unchanged.

## Deletion contract

- DELETE /api/sidebar-threads/{thread_id}?session_id={session_id} returns 204 with no body.
- The supplied session selects SQLite directly. Ownership is checked before writing the deletion marker.
- No session enumeration, Turn history loading, or sidebar summary generation occurs in deletion.
- Task admission and deletion share the queue lock. Deleted conversations reject new work, including after restart.
- Queued work is discarded and running thread jobs receive cancellation immediately. Slow command-tree and terminal cleanup runs in a backend-owned executor.
- GET /api/sidebar-threads/{thread_id}/deletion reports pending, completed, failed (with error_report), or unavailable. Missing results are not reported as successful cleanup.
- Cleanup covers the selected conversation and its Agent descendants. Other branches and persisted Turn history remain untouched.
- The frontend deletes unsaved drafts locally, prevents duplicate requests, waits for HTTP success before removing the row, and filters late updates for deleted conversations.

## Model commands

- Removed maximum-limit reservations and the fixed sandbox resource-wait timeout.
- The Broker samples each registered model-command Windows Job Object every 250 ms, including children, using current private committed memory rather than historical peak memory.
- Defaults: min(8192 MiB, 80% physical memory), 512 processes, 32768 handles. No aggregate CPU limit.
- Settings expose aggregate limits separately from individual command limits. GET /api/settings/sandbox/resources reports usage, limits, queued count, and sampling errors.
- FIFO admission grants one launch at a time. The first successful sample is required before another launch can be admitted.
- Live waiting requests have no deadline and hold no maintenance lease. Abandoned, ungranted tickets expire only after their polling heartbeat disappears; granted tickets are not expired during launch preparation.
- Exceeding a limit stops the newest model-command tree, waits for sampled exit, and only then considers the next tree. write_stdin does not change ordering.
- Failed samples preserve the last known usage and stop admission. Failed termination retains accounting and reports the original error.
- Right-panel manual terminals do not participate in aggregate accounting or aggregate-limit termination. Conversation deletion still closes owned manual terminals.

## Verification

- Ruff check and format check: passed (484 Python files).
- Frontend typecheck: passed.
- Frontend Vitest: 57 files / 453 tests passed, using one worker (236.59 seconds).
- Frontend build: passed (15.95 seconds); the existing bundle-size warning remains.
- Final focused backend regression: 97 passed (33.81 seconds), including all 15 new deletion/resource tests, command/cache behavior, sandbox contracts, and job registry behavior.
- Deleting a running local command: HTTP 93 ms in the latest measured run; owned process cleanup completed asynchronously.
- Deleting a 250-Turn conversation among 101 sessions: HTTP 78 ms in the latest measured run (94 ms in an earlier run). SQLite history and a sibling sidebar branch remained present. The test fails explicitly if deletion enumerates sessions, loads complete history, or rebuilds a summary.
- Real Windows tests cover three simultaneous low-usage commands, current memory falling after a 32 MiB release, aggregate process limits, and newest-first termination.
- Fault/control tests cover FIFO cancellation, failed sampling, failed termination, retryable cleanup with the original exception, pending-work cancellation, restart rejection, slow launch preparation, and settings not being overwritten by later runtime initialization.
- Desktop (1365 x 900) and narrow-screen (390 x 844) browser checks passed against an isolated HTTP/SQLite instance, including deletion and resource-setting persistence. The final browser test also opened a second page and verified that its refreshed list removed the deleted conversation. It passed in 9.97 seconds; desktop deletion took 260 ms from confirmation to row removal.
- Full backend run: 1315 passed, 49 skipped, 2 failed (383.71 seconds). The final launch-cancellation refinement was then covered by the 97-test regression above. Skips include opt-in Docker/download tests, unavailable isolated MCP v1, and platform/privilege-specific cases.

Known existing backend failures, left unchanged:

- tests/test_agent_threads.py::test_legacy_schema_is_rejected_without_mutating_or_deleting_database: main already requires v17 while the test still expects v16.
- tests/test_agent_threads.py::test_running_subagent_bridge_accepts_live_runtime_config: persisted permission intermittently remains read_only instead of workspace_write. Repeated isolated runs against the unchanged main code reproduced the identical assertion at line 1782. An earlier worktree full run passed this test; it is not counted as a final pass.

No schema or unrelated runtime-configuration fix was included.
An initial attempt to run several heavy suites together exhausted available Windows memory; those incomplete runs were not counted as successful. Verification was repeated serially.
The installed Broker has not been updated or restarted. Its deployed behavior is not claimed to match this worktree yet.
The browser fixture uses an isolated HTTP server and SQLite directory; its resource-status provider is a test adapter, not the installed privileged Broker.
Real Windows Job Object tests run separate local processes without using the installed sandbox accounts or service.
