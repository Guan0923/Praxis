# Sandbox recovery: start before repair

## Behavior

- The existing POST /api/sandbox/repair endpoint now uses Windows service state before choosing an action. POST /api/sandbox/install uses the same decision path.
- A missing service follows the existing installation path. A stopped service is started without changing service configuration, accounts, ACLs, credentials, or readiness files.
- Pending start/stop states receive up to 10 seconds to settle. A started service receives up to 10 seconds of health observation before a running-but-unhealthy service can enter the existing repair transaction.
- A healthy service does not run repair. Confirmed startup failures and service-state query failures pause automatic recovery instead of escalating to repair.
- Resource-manifest checks required for full repair occur after service startup. Existing maintenance exclusion and active-command protection remain in place.
- GET /api/sandbox/status is read-only and includes service_state. Stopped services are reported directly without probing a nonexistent pipe.
- Native status-pipe probes use cancellable overlapped I/O with a default one-second timeout, bounded by the remaining startup observation window. Other command I/O is unchanged.
- Startup uses the existing elevation and protected error-report channel only when needed. The helper start operation returns before all installation steps.

## Frontend

- The Check and Overwrite Repair buttons, confirmation dialog, and manual repair handler were removed.
- Automatic recovery retains the 10-second initial observation, 10-second post-repair verification, and 30-second healthy polling schedule.
- Startup failure, service query failure, and UAC cancellation pause the current page. Polling continues read-only, retaining the original failure until health returns. Reloading the page allows a new observation cycle.
- Recovery is labeled as recovery, not repair. The resource panel only polls a healthy sandbox; otherwise it states that usage is unavailable.

## Verification and deployment

- Worktree: mini_agent-delete-resource-usage; branch: codex/conversation-delete-resource-usage. Earlier uncommitted changes remain included.
- No commit, merge, push, or production-service restart was performed.
- Real Windows tests used uniquely named temporary services and pipes. They verified successful service startup, missing-executable error 2 without repair, unchanged production Broker status, valid pipe replies, and cancellation of unresponsive status probes. Temporary services were deleted afterward.
- Unit and HTTP tests cover startup state transitions, failure reports, query failures, pause behavior, healthy no-op recovery, and startup bypassing unnecessary repair-manifest work.
- Ruff check and format check passed (488 Python files). Frontend typecheck and build passed; the build retains the existing large-chunk warning. Full Vitest passed: 57 files, 453 tests.
- Full backend run: 1340 passed, 49 skipped, 2 failed in 364.08 seconds. One failure is the existing test_agent_threads legacy-schema assertion expecting v16 while the implementation requires v17. The other was the new browser fixture registering probe routes after the static frontend mount; the fixture routing and ambiguous text selectors were corrected.
- After those test-only corrections, all 24 startup tests passed and the browser test passed separately in 52.45 seconds. The whole backend suite was not rerun after the fixture-only corrections. The earlier intermittent live-runtime permission test passed in this full run.
- Real browser verification used isolated local HTTP and SQLite, with a controlled failing Broker rather than the production service. Desktop (1365x900) and narrow (390x844) screenshots confirmed removed buttons, paused status, and readable expandable error details. A real 31-second wait confirmed that paused polling retained the original error and did not repeat recovery. The same fixture also verified deletion across two pages and resource settings.
- Local verification logs: .test-tmp/start-first-backend-full.log, .test-tmp/start-first-final-targeted.log, .test-tmp/start-first-browser-final.log, .test-tmp/start-first-frontend-final.log, and .test-tmp/start-first-build.log.
- Elevation cancellation and pending/failed state transitions are covered by controlled tests. No real UAC prompt was accepted or cancelled, and the existing production installation/stop transaction was not exercised.
- These tests do not claim that the installed production Broker has been updated or started. Its deployed startup and existing installation data were not exercised by the temporary-service probe.
