# Original Error Reports: Change and Verification Record

Date: 2026-09-10
Branch: codex/backend-original-errors
Base: main at b674d883
Worktree: C:/Users/Administrator/Desktop/project/mini_agent-worktrees/backend-original-errors

## Scope

This work changes exception propagation and diagnostic output. It does not change the sandbox automatic-repair retry policy, install or restart the production Broker, migrate old session records, or modify the original working tree. No commit, merge, or push was made.

## Removed Replacements

- Sandbox native account, token, desktop, job, ACL, process and credential helpers: removed paraphrasing wrappers around native failures. Broker pipe I/O and JSON decoding now propagate the original exception.
- Broker status: removed message-substring classification; use explicit operation stages, native error numbers and protocol status metadata. Retain key/configuration/readiness failures rather than treating them as ordinary absence.
- Runtime hooks: removed HookExecutionError and preserve the callback exception with hook metadata. Non-hook execution errors continue propagating. After-run cleanup cannot replace the original execution failure.
- Providers: parse errors are classified at the parser boundary, without a second conversion in response preparation. Memory scheduling uses the original provider status/configuration error to retain quota cancellation and retry behavior.
- Tools and jobs: removed the registry, subagent and command layers' replacement exceptions. Jobs retain the exception object inside the process and publish a sanitized report. Command exit codes and bounded stderr/stdout notes survive tool execution; timeout conversion retains TimeoutExpired as its cause.
- MCP: connection/subscription futures receive the original exception, failed servers retain exception objects, and already-classified ToolError instances are not wrapped again. Independent server failure isolation remains.
- HTTP: route catches return an error response directly. Helpers that must preserve HTTP status attach API status metadata to the existing exception instead of creating a second HTTPException. The HTTP boundary logs only the sanitized report.
- File operations and persistence: removed redundant primitive conversions and repeated configuration-error wrappers. File rollback, helper-result cleanup and process cleanup retain the initial failure.

## Retained Necessary Boundaries

- ModelTransportError: transport status, retryability, stream-start and cancellation decisions.
- ProviderOutputError / ModelOutputError: provider output validation and model-output retry decisions at the origin boundary.
- TracePersistenceError: stop a Turn when required audit persistence fails.
- MemoryConflictError / MemoryStorageError and SessionFileError: storage conflict and file API control where a raw operation needs a business classification.
- ToolError: MCP/tool-domain failures at the first required boundary; upper tool/runtime layers no longer repeat the conversion.
- BrokerInstallationError and authenticated Broker error reconstruction: preserve control codes and the server/helper error_report. The receiving process does not invent a replacement stack.
- Active validation, permission refusal, cancellation and cleanup-pending conditions remain business errors. Third-party exception chains are not rewritten.

## Report Contract

The existing domain error module generates error_report with type, message, traceback and optional errno/winerror. Origin type/message come from the active cause chain. A report received from another process is normalized and retained, not regenerated from the receiving line.

Tracebacks contain file, line, function and causal relationships, without source lines or local-variable dumps. Credential-shaped values, authorization/cookie headers and URL credentials are redacted before output. Reports are bounded with an explicit truncation marker; the tail retains the root exception location.

HTTP preserves status, detail and programmatic code. Turn JSON, tool failures, jobs, Broker status and SSE carry the same report shape. Newly recorded Turn errors survive SQLite reload; old records are not migrated or given invented frames.

The elevated helper uses a request-unique result filename inside the validated Broker directory. Reparse paths are rejected, output is created with a protected Windows DACL and atomically replaced, and only the current request file is removed. A missing report explicitly shows the actual exit code and states that child exception details were not obtained. This mechanism was tested without running the actual elevated installer.

## Frontend

ApiError and state retain error_report. ErrorDisplay renders the original type/message as text and offers a collapsible, scrollable, copyable plain-text stack. It is used for chat/Turn errors, tool results, sandbox settings, provider/MCP/Skill settings, sidebar settings, file operations, Trace and benchmark failures. Control codes remain available to code but do not replace the diagnostic summary.

## Verification

The complete backend run finished with 1273 passed, 49 skipped and 10 failed. After correcting the status-code regression and updating the old wrapper-dependent expectations, re-running the failed cases produced 7 passed and the 3 pre-existing failures listed below. A final 314-test boundary suite passed. These are separate runs, not an invented all-green full-suite result.

- Real temporary-file access failure through HTTP, real SQLite failure and durable Turn reload.
- Local HTTP 503 failure with query-credential redaction; no paid provider calls.
- Independent Windows named pipe using the real authenticated Broker dispatcher; server-origin frames survive the client and test cookies do not appear in the report or audit file.
- Actual child process writes and parent reads a protected diagnostic file; an early child exit without a report is reported honestly.
- Actual command exits nonzero with sensitive stderr; exit code, original exception and redacted output are retained.
- Static ErrorDisplay browser verification at 1280x800 and 390x844: expansion, copying, no horizontal overflow and no page errors. Screenshots are in .test-tmp/error-report-desktop.png and .test-tmp/error-report-mobile.png.
- Frontend: 446 tests passed in the final full run (54 files). TypeScript checking and the production build passed.
- Backend final boundary checks: 314 passed, covering reports, real commands, MCP, Broker installation/report handling, maintenance exclusion, hooks, subprocesses, memory storage and Turn HTTP/SSE. Earlier command/cleanup checks also passed 146 tests.
- Ruff excludes generated .test-tmp fixtures, which deliberately contain invalid Python; source checks and format checks passed. The initially requested bare-dot check included those generated fixtures and failed, so it is not reported as a pass.
- Production build passed with the existing large-chunk warning. npm installation reported existing dependency audit advisories; dependency upgrades were not part of this task.

## Existing Failures and Limits

Reproduced in the original working tree without source edits:

1. test_legacy_schema_is_rejected_without_mutating_or_deleting_database expects v16 while the implementation requires v17.
2. test_baseline_small_chunk_latency_and_outbox_size compares equal 479-byte outbox measurements with a less-than-one-tenth expectation.
3. test_running_subagent_bridge_accepts_live_runtime_config sometimes reads read_only immediately after a workspace_write update. It also failed in the original working tree during this verification.


Docker/download acceptance, the isolated MCP-v1 environment and platform-inapplicable tests retain their existing skip conditions. Actual UAC installation, privileged account/ACL/network repair and production Broker lifecycle changes were not executed. This work proves diagnostics in isolated local chains, not a successful repair or deployment of the running sandbox.
