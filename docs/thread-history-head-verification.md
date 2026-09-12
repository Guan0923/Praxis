# Backend-owned thread heads

Verified on 2026-09-12 in branch codex/backend-thread-head-20260912,
based on main at 080216fd. No commit, merge, or push was made.

## Contract

- GET /api/turns/history returns current_turn_id, turns, next_cursor, has_more.
- The storage reader holds a read transaction while reading the thread head
  and its ancestor nodes. Empty threads return a null head.
- Latest-page loads use that head; older-page loads retain the displayed head.
- Frontend callers retain the complete page response. They do not select leaves,
  sort timestamps to find a head, or construct pagination cursors.
- Subagent snapshot/delta events include current_turn_id. Historical snapshots
  cannot move the display to an ancestor. New-head events can advance it.
- Main chat, side chat, fork, recovery, refresh, and subagent views use these rules.

## Automated checks

- Frontend full suite: 60 files, 468 tests passed.
- npm run typecheck: passed with worktree-local npm ci dependencies.
- npm run build: passed; existing large-chunk warning remains.
- Backend focused suite: 97 passed (history head, runtime SSE, turn protocol,
  view-state synchronization, conversation cache/commands).
- After adding the SSE head assertion: history-head and runtime SSE suites,
  14 passed.
- Changed Python files: Ruff check and format check passed.
- A broader run including test_agent_threads.py exposed an unchanged assertion
  expecting schema v16 while main requires v17. This unrelated test was not edited.

## Real HTTP and browser checks

The fixture uses a separate data root and the real SQLite store, HTTP routes,
application SSE, subagent SSE, and built frontend. It never invokes a model or
changes Broker configuration. No production data or existing service was changed.

Start a fresh fixture from the worktree root:

    conda activate dev
    uv run python -m tests.serve_thread_head_fixture --data-root .test-tmp/browser-new --port 8028

Read thread-head-fixture.json in that data root for the generated identifiers.
The test-only POST /fixture/advance/{thread_id} persists and publishes a new turn.
Use a new data-root directory for each run.

Playwright checked:

- Main chat initially loads five turns, then loads older ancestors on scroll.
- Advancing the backend head updates the already-open main chat.
- Selecting the child through the Thread tree loads its own head and history.
- Child pagination succeeds; parent-thread messages stay out of its display.
- A newly published child turn appears without changing the root conversation.
- Selecting an older turn's version via the real PATCH route retains the latest head.
- Forking via the real POST route returns 201; the new page uses the fork head.
- Desktop 1440x1000 and mobile 390x844 render without horizontal overflow.
- No browser page errors occurred in the final run.

Final screenshots are in .test-tmp/history-final-desktop.png,
.test-tmp/history-final-child.png, and .test-tmp/history-final-mobile.png.
These generated artifacts are not intended for commit.
