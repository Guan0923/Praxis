# Conversation controls and read performance

Verified locally on 2026-09-12. Changes are uncommitted. The user's normal backend was not restarted and existing conversation databases were not modified.

## Causes and changes

- Queue ownership and Turn lookups previously called list_sessions(), which rebuilt every conversation and replayed its text deltas. Browser Turn operations now carry the server-verified session to the handler. Queue ownership uses the thread index or its specific session database. Unscoped local lookups enumerate database locations without building conversation summaries.
- Sidebar summaries previously loaded complete RuntimeState objects and cloned their histories. They now read ancestry and visible-message counts directly, within one SQLite read transaction. The sidebar endpoint no longer lists all summaries twice.
- Draft saves, Fork, and unrelated mutations shared a session-wide frontend request queue. They now use resource-specific groups. Session ownership, local Origin protection, and mutation ordering for the same resource remain enforced.
- Fork now returns the branch and its initial history in one HTTP response. Creating a fork no longer loads every Turn just to locate its parent. Repeated clicks share the UI's pending operation instead of dispatching another Fork.
- Catalog refresh overwrote lastNodeId with an unhydrated summary's undefined value. The loaded head is now preserved. If a new-message request omits its parent, the backend continues from the thread's stored head, not the session root.
- Missing ancestry no longer silently truncates displayed history or replaces model context with the current Turn alone. Context reads walk the actual parent chain rather than loading unrelated branches.
- Right-panel layout and windows belong to a conversation thread, including Fork branches within the same physical session. Responses from a previous selection cannot appear in the new selection. Background panel reads do not disable creation buttons.
- Dragging a sidebar row suppresses the ensuing selection click. It does not change the selected URL or reload conversation history.
- Runtime checkpoint writes no longer trigger catalog/history refreshes. Repeated checkpoints of the same run and reason replace their previous snapshot. Runtime deltas are folded into the canonical Turn every 256 frames, bounding replay work without losing output. Old checkpoint backfill scanning was removed from the message-write path.

## Read-only comparison

Run from the repository root:

~~~powershell
.venv/Scripts/python.exe -m tests.support.profile_conversation_reads
~~~

This compares HEAD mixins with the working tree on the same existing local data, using SQLite mode=ro. It measures query work, not HTTP overhead, database initialization, or model latency. Values are medians of three runs:

| Read | HEAD | Working tree |
| --- | ---: | ---: |
| Queue ownership | 1552.07 ms | 4.26 ms |
| Sidebar catalog | 814.84 ms | 318.86 ms |

The initial profiled ownership call reconstructed 18 node lists across 9 sessions and applied roughly 60,000 deltas. That profiling run took 2.188 seconds; it is not the uninstrumented baseline above.

## Database size

Read-only inspection found roughly 29,800 retained runtime deltas in one session. Another session contained 813 checkpoint snapshots totaling about 43 MB of JSON characters. Some databases also had substantial free pages retained for reuse. SQLite file size is not the amount of user-entered text.

The changes bound future growth. No VACUUM, deletion of existing checkpoints, or rewrite of user history was performed.

## Real browser verification

Used the real app, HTTP, WebSocket operation control, SSE, and a loopback simulated model with isolated data. No paid model API was called.

~~~powershell
.venv/Scripts/python.exe -m tests.support.queue_control_server 8127 .test-tmp/interface-browser-20260912
cd frontend
node browser-tests/conversation-controls.mjs
~~~

Representative completed run, measuring the UI action through HTTP acknowledgement:

| Operation | Time |
| --- | ---: |
| Send | 430 ms |
| Fork | 238 ms |
| Queue create / edit / delete | 128 / 119 / 85 ms |
| Batch queue send | 67 ms |
| Pause / resume | 187 / 420 ms |
| Rewind | 207 ms |
| Sidebar reorder | 392 ms |

- Queue edit, delete, batch acknowledgement/removal, pause, resume, and Rewind completed without the operation-pending error.
- A message sent after Fork retained the fork's parent.
- The final rewound version reached success, not merely HTTP acceptance.
- Main conversation and branch right panels were isolated in the browser; returning to the branch restored its files tab.
- Real mouse reordering produced zero history GET requests and preserved the current URL.
- No browser page errors were observed. Desktop and mobile screenshots are under .test-tmp/interface-browser-20260912/. Mobile panel geometry was checked against the full viewport after its opening transition.

## Regression checks

- Backend control/storage selections: 107 passed; a subsequent overlapping selection after the final context changes: 97 passed. Counts are not additive.
- Frontend five-suite selection: 111 passed; the final AgentApp/right-panel selection including the new catalog-head test: 25 passed.
- Production frontend build, scoped Ruff, and git diff --check passed.
- Typecheck still reports the same six pre-existing errors in settings.models.test.ts, runController.test.ts, and versionSelection.test.ts.

## Existing affected history

Read-only inspection of session_6e251693f05048ce95f1988845753481 found no absent stored parent node. Its latest main Turn, turn_c5bf76fc2c03469eaeaef345c1c43908, points to the session root instead of the earlier main-chain head turn_eef9369b05ec49ce885ab855de94cd83. Earlier Turns remain stored.

The source defect that can create this disconnection is fixed. Rewriting that already-stored relationship is a separate data repair awaiting confirmation of the intended ancestry and a backup; it has not been performed.
