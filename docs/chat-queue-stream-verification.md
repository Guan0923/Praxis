# Chat Queue and Stream Verification

Date: 2026-09-12

## Changes

- Consume handled composer keys before Lexical can also insert a newline.
- Restore the latest projected draft, not a stale render's draft; update that
  draft before triggering local React state updates.
- Wait for queue mutations to finish before automatic or manual batch sending.
- Refresh queues on backend create, edit, delete, dispatch, acknowledgement,
  release, and application reconnect notifications.
- Register pause control for every Turn in one execution; route steering to
  that execution's mailbox and release its unconsumed inputs at completion.
- Resolve active pause/steering requests from the live Turn, without replaying
  SQLite deltas or returning the entire conversation as a pause response.
- Read new stream entries from the tail and avoid database-backed cache
  registration on every text delta.

## Real Local Verification

Used an isolated data root and the real built frontend, HTTP, WebSocket, SSE,
LLM client, and a loopback-only simulated model. No paid model requests.

- Enter after sending and queueing leaves an empty composer.
- Queue edit restores text; modified text is resubmitted successfully.
- Automatic sending produced one model request containing
  "batch second\n\nbatch first edited" and removed both entries.
- Forced batch sending interrupted the original model connection, produced
  one request containing "force one\n\nforce two", and removed both entries.
- Pause changed the page to paused and the simulated model recorded a closed,
  interrupted connection.
- A real HTTP DELETE returned 204; the open page removed the entry without a
  reload or a new Turn frame. Button dispatch is covered separately in tests.

## Event Read Measurement

250 incremental reads per sample; one newly published event per read.
Both versions returned exactly that new event.

| Retained history | Before median | After median |
| --- | ---: | ---: |
| About 100 events | 0.0745 ms | 0.0086 ms |
| About 9000 events | 2.1008 ms | 0.0089 ms |

This measures the event buffer only, not total model-to-screen latency.

## Automated Checks

- Queue, controls, Turn protocol and operation-control selection: 91 passed.
- Steering, Turn protocol, runtime event transport and incremental runtime: 81 passed.
- ChatPage, queue refresh, run controller and draft-state selection: 108 passed.
- Final queue-specific selection, including pending batch saves: 22 passed.
- Production frontend build and scoped Ruff check passed.
- Full TypeScript checking has existing errors in settings.models.test.ts,
  runController.test.ts and versionSelection.test.ts; compare with main.

The user's normal backend/frontend services and data were not restarted,
reconfigured, or used for simulated-model verification. No commit or push.
