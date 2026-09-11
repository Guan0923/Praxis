# Internal message and configuration flow

## Status

Implementation is in codex/unified-internal-message-flow, based on main at 3e39e2d1, in mini_agent-unified-message-flow. Main and its existing uncommitted files are untouched. No commit, merge, push, or production service restart was performed.

**Not ready to merge: one design decision remains open.** Removing all deduplication digests while preserving content-conflict checks retains acknowledged message bodies in receipt comparison keys after conversation-cache eviction. The new eviction regression test exposes this. Approval was requested to retain a small digest for evicted delivery identities, without whole-message JSON serialization. That exception has not been implemented without approval.

## Completed changes

- Immutable InputMessage and FileReference values carry content through queued messages, deliveries, mailboxes, startup workers, ConversationService and the node bridge.
- Delivery fields distinguish start commands, configuration, routing, reply requests and report status. The generic delivery payload dictionary and its to_dict/from_dict transport round trips are removed.
- Mailboxes return InputDelivery values with explicit acknowledgement, not temporary dictionaries containing an _ack function. SQLite persistence and execution-boundary handling still precede acknowledgement.
- Shared frozen execution/model settings live in domain rather than API modules. Startup config is projected from validated request fields without revalidation and passed directly to the worker and stream.
- RuntimeConfigUpdate is separate from a complete startup config. UI patches retain omitted-field and model-merge semantics and still apply at the next model/tool boundary.
- HTTP messages, queue API responses, SSE frames, canonical SQLite history and provider wire shapes are unchanged. Conversion remains at those boundaries. Public embedding run_task converts its string input once; internal callers use run_message.
- Ordered queued-message merging and reference deduplication remain business operations, not serialization steps. Permission checks, cancellation and task ownership remain in place.

## Verification

- Ruff passed after scoped formatting.
- Frontend typecheck, all 453 Vitest tests across 57 files, and build passed. Existing large-chunk build warning remains.
- Focused backend run: 87 passed, including configuration object identity, immutable nested references, real SQLite query-only failure without acknowledgement, and model/tool steering.
- Real browser run: passed in 19.87 seconds using an independent backend port/data directory and local HTTP model server. Two Turns completed; desktop, narrow viewport, refresh and second-page history passed. The production Broker was not used.
- Earlier full backend run: 1330 passed, 56 skipped, 4 failures. Two were legacy string-based steering tests, now migrated and covered by the passing focused run. Existing failures were the schema-v16 assertion versus schema v17 and the intermittent subagent live-configuration persistence check.
- Final backend run: 1346 passed, 49 skipped, 2 failed in 410.39 seconds. Failures: test_legacy_schema_is_rejected_without_mutating_or_deleting_database (existing v16/v17 assertion), and test_acknowledged_comparison_does_not_retain_evicted_message_body (new, unresolved receipt-retention regression). The intermittent subagent live-configuration check passed in this final run. The new failure is not skipped or marked as passing.
- Final backend log: .test-tmp/backend-final09.log. Browser log: .test-tmp/browser08.log. Frontend logs: .test-tmp/frontend-typecheck.log, frontend-tests.log and frontend-build.log.
- The final run also covers the internal ConversationService.run_message entry and the unchanged public embedding run_task boundary.

Logs are under .test-tmp in this worktree; browser screenshots are in the unique pytest temporary directories. Tests use no paid model API.
