# Conversation navigation verification

## Scope

- Branch: `codex/conversation-state-navigation-20260911`.
- Worktree: `C:/Users/Administrator/Desktop/project/mini_agent-state-navigation`.
- Base: `main`, commit `3e39e2d1`.
- Original checkout changes were preserved. No commit, merge, push, or original service restart.
- Browser view state is stored in the existing session SQLite database. Browser copies are in memory; unconfirmed writes remain pending on failure.
- Conversation URLs, backend event replay, per-Turn immediate version selection, five-idle-conversation caching, and five-Turn initial loading are implemented.

## Automated checks

- Frontend: 463 tests passed, zero failed; `npm run typecheck` passed.
- Production: `npm run build` passed. The existing large bundle warning remains.
- Focused backend: 66 passed (`test_view_state_sync.py`, `test_turn_protocol.py`).
- Broader backend run before the final two added tests: 99 passed, one failed across view-state, Turn protocol, and agent-thread tests.
- The failure expects a v16 schema error but main reports v17. Reproduced on an isolated archive of the exact base commit; not changed here.
- An earlier run also exposed an intermittent live-config persistence test failure. The same failure was reproduced by repeating that test on the exact base commit. It passed in the final broader run.
- Ruff checks passed for changed Python files. `git diff --check` passed.
- Additional coverage includes stale version responses, failure with a newer click, preserving new Turn fields, silent stream disconnects, failed snapshot handling, expired event positions, and no-op panel writes.

## Real local HTTP and browser checks

All data was created by `tests/serve_navigation_fixture.py`; no paid model API was called.

| Check | Observed result |
| --- | --- |
| Initial conversation | One history read, five latest Turns |
| A -> benchmark -> B -> A, then browser back/forward | Zero document navigations; one history read for uncached B |
| Main conversation DOM across B and back | Same ChatPage element, no remount |
| Sixth idle conversation then return to evicted A | A read once again, five initial Turns |
| Version response delayed by 800ms | Visible change in 133ms in development and 151ms in production; did not wait for save |
| Rapid version clicks | Three clicks ending at the first pending target sent one PATCH; final backend index matches last click |
| Delayed rejected version save | Immediate tentative version, rollback to confirmed version, visible independent error |
| Continuous typing | 20 characters at 40ms intervals produced one draft PATCH after typing stopped |
| Backend stopped then restarted using the same fixture data | Unsynchronized indicator, retained content, reconnect with one current history read |
| Main and child drafts | Separate backend values and separate URLs |
| Uploaded attachment | Real local file upload; completed reference restored after refresh |
| Reasoning expansion | Saved and restored after refresh |
| Reading position | 1400px restored; older 20px anchor restored by initially reading five Turns then one older page |
| Invalid/archived root URL | Explicit unavailable message, unchanged target URL, trash entry; no unrelated fallback |
| Child URL under a wrong root | Rejected; child content not shown |
| Production deep links | Main, child, benchmark, and trash load directly; missing API/static resources remain 404 |
| Desktop and narrow screen | Inspected at 1440x1000 and 390x844; narrow-screen document width remains 390px |

Timing values are individual local measurements including Playwright action overhead, not a percentile benchmark or a performance guarantee. Running conversations are covered by cache tests; no actual model task was started. The registered Sandbox Broker points to the original checkout, while this worktree expects its own interpreter path. That service registration was not modified. Benchmark page navigation was checked, not benchmark execution.

## Local preview and evidence

- Production preview: `http://127.0.0.1:8018/`.
- Fixture-only data: `.test-tmp/live-data2`; real user data is not used.
- Frontend test output: `.test-tmp/frontend-final.json`.
- Screenshots: `.test-tmp/navigation-desktop.png`, `.test-tmp/navigation-mobile.png`.
- The temporary Vite server on port 5188 was stopped after verification.

To start the fixture again from this worktree (only when port 8018 is free):

```powershell
conda activate dev
$env:PYTHONPATH = (Get-Location).Path
$env:PRAXIS_ALLOWED_ORIGINS = 'http://127.0.0.1:8018'
uv run python tests/serve_navigation_fixture.py --data-root .test-tmp/live-data2 --port 8018
```

The source changes and this report are intentionally uncommitted. Temporary evidence and fixture data under `.test-tmp` should not be included in a code commit.
