# Agent report handoff

## Scope

Implemented on the existing codex/unified-internal-message-flow worktree, preserving the previous internal-message refactor and unrelated changes. No commit, merge, push, application service restart, paid model call, or production sandbox operation is part of this work.

## Single-send delivery

- Removed `_ensure_report_dispatcher`, `_report_dispatch_loop`, `_dispatch_ready_reports`, their thread/events, the 0.5-second database scan, and resend backoff.
- Removed the unused `reply_subagent_message` intermediary/thread-local context and the separate `mark_agent_turn_report_state` write.
- Terminal delegated Turns, requested replies, and paused Turns marked complete use the same report publication entry.
- Report publication holds the queue admission lock. SQLite writes report content and queued status within an uncommitted transaction; queue entries remain unclaimable while notification callbacks are prepared.
- Preparation or commit failure rolls back the SQLite transaction and removes only this publication's staged queue entries. The original exception escapes; there is no automatic resend.
- After SQLite commit, entries become claimable and recipient notifications run. A failed recipient notification does not suppress notifications to other recipients.
- Report messages no longer enter the generic delivery-comparison receipt cache. A repeated publication of a finalized report Turn is an explicit programming error, not a silently deduplicated send.

## Active reception

- Both root and child runners register their active recipient and operation-abort callbacks for the duration of a run.
- A report interrupts the current model request, not the parent task. The next safe boundary consumes reports before requesting another answer. Partially generated tool calls are not executed.
- Running tools use their existing cooperative cancellation/abort support. Non-interruptible operations finish; completed results are retained even if a report arrived before the handler returned.
- The old tool batch stops admitting further calls. Approval remains a user decision; a pending approval is not automatically granted, and its obsolete operation cannot execute after the report arrives.
- Idle recipients save report history once. Paused recipients keep their existing resume policy. Cancelled/deleted/closing recipients do not acquire a new task because of a report.
- Startup, report staging, and deletion share queue admission. A queued user Turn keeps reports for that incoming task rather than prematurely draining them into an idle recipient.
- On task exit, only that recipient's in-memory report queue is checked. There is no database or all-session recovery scan.

## Receipt transaction

For a running recipient, the node writer flushes prior deltas, then commits the report Item, delivered status, and next SSE frame sequence in one SQLite transaction. Only afterward does it update its live snapshot, publish the frame, update model context, and acknowledge the queue entry. Idle receipt uses the same history/status transaction without an active stream frame.

Failures after receipt starts are execution failures: history is not rolled back and the report is not resent. A failed claim is not made available automatically and cannot repeatedly interrupt subsequent model calls. Existing generic-message retry/dedup behavior is outside this change.

HTTP/SSE schemas, report text, formal history, and trace formats remain unchanged. The process can roll back before database commit, but SQLite and RAM cannot share crash atomicity: after commit, an abnormal exit keeps disk records and drops the in-memory queue; restart does not resend them.

## Verification

Tests use isolated data directories and local model HTTP endpoints; real subprocess tests operate only on their own child processes.

- New report tests: 16 passed (4.02 seconds).
- Final full backend run: 1361 passed, 49 skipped, 3 pre-existing failures (269.98 seconds). Log: .test-tmp/report-final-backend03.log.
- Ruff check and format check passed for all 493 source/test Python files, excluding .test-tmp generated test fixtures. Running bare Ruff after pytest also encounters the deliberately invalid Python files created by glob/grep/oracle tests in that directory; no source lint errors were suppressed.
- Frontend typecheck passed; Vitest passed 57 files / 453 tests with one worker (192.94 seconds); Vite build passed (8.79 seconds). Existing React act/jsdom/Ant Design warnings and the large-chunk build warning remain; frontend source was not changed.
- git diff --check passed. The main checkout's pre-existing modified/untracked files were preserved.

Covered cases include SQLite write/enqueue/notification-preparation/commit failures, consumption rollback, unread single-send reports, FIFO arrivals during consumption, cancellation, queued startup, deletion/close rejection, restart without replay, Agent/Plan model-stream interruption, incomplete tool arguments, a real Python child process, non-interruptible work, and pending approval.

## Pre-existing issues outside this change

- The legacy database rejection test expects schema v16 while the current application requires v17.
- The live subagent configuration test intermittently observes read_only instead of workspace_write. The existing main-baseline-8.log in the earlier worktree records the same failure before this change.
- The previous internal-message refactor retains acknowledged generic-message body values in its comparison cache. Its existing eviction regression remains open. Reports no longer use that cache; other message types are deliberately unchanged.
