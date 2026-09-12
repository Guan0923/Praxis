import { afterEach, describe, expect, it, vi } from "vitest";

import { pauseTurn, SseExecutionError, streamAttachedTurn, streamChat, streamResume } from "../api";
import type { Conversation, RuntimeStateNode } from "../types";
import { createRunController } from "./runController";
import { TURN_PROTOCOL_VERSION } from "./runtime/runtimeNodeNormalization";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    pauseTurn: vi.fn().mockResolvedValue(undefined),
    streamChat: vi.fn(),
    streamAttachedTurn: vi.fn(),
    streamResume: vi.fn(),
    streamRewind: vi.fn(),
  };
});

function turn(): RuntimeStateNode {
  return {
    thread_id: "session_1",
    parent_thread_id: "",
    session_id: "session_1",
    parent_session_id: "",
    id: "turn_1",
    parent_id: "",
    version: TURN_PROTOCOL_VERSION,
    firstKeptItemSize: 8,
    compactionId: "turn_1",
    user: "user_1",
    provider_name: "local",
    model: { reasoning_effort: "medium", current_model: "test", context_length: 4096, output_length: 512, thinking: "enable", temperature: 0 },
    permission_mode: "read_only",
    running_mode: "agent",
    usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0, reasoning_tokens: 0, total_tokens: 0 },
    cwd: "C:\\workspace",
    project_cwd: "",
    timestamp: "2026-08-26T00:00:00Z",
    status: "running",
    current_data_idx: 0,
    data: [[
      { role: "user", content: [{ type: "text", text: "hello", status: "success" }] },
      { role: "assistant", content: [{ type: "text", text: "", status: "running" }] },
    ]],
  };
}

function request() {
  return {
    conversationId: "conversation_1",
    sessionId: "session_1",
    threadId: "session_1",
    turnId: "turn_1",
    prompt: "hello",
    resume: false,
    mode: "agent" as const,
    permissionMode: "read_only" as const,
    reasoningEffort: "medium" as const,
  };
}

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function deferred<T = void>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function controllerCallbacks() {
  return {
    activeRuns: new Map(),
    updateLastMessage: vi.fn(),
    updateConversation: vi.fn(),
    rebindRunSession: vi.fn().mockResolvedValue(undefined),
    refreshSessions: vi.fn().mockResolvedValue(undefined),
    refreshQueuedMessages: vi.fn().mockResolvedValue(undefined),
    recoverConversation: vi.fn().mockResolvedValue(undefined),
    onControlError: vi.fn(),
  };
}

describe("run controller lifecycle", () => {
  it.each([
    { status: "paused", resume: true },
    { status: "paused", resume: false },
    { status: "success", resume: false },
  ] as const)("replaces a $status subscription without EOF for an explicit run (resume=$resume)", async ({ status, resume }) => {
    let oldSignal!: AbortSignal;
    vi.mocked(streamChat).mockImplementationOnce(async (_prompt, onMessage, signal) => {
      oldSignal = signal;
      onMessage({ type: "turn.snapshot", revision: 0, turn: { ...turn(), status } });
      await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
      return "aborted";
    });
    if (resume) vi.mocked(streamResume).mockResolvedValueOnce("completed");
    else vi.mocked(streamChat).mockResolvedValueOnce("completed");
    const callbacks = controllerCallbacks();
    const controller = createRunController(callbacks);
    const first = controller.runConversation(request());
    expect(oldSignal.aborted).toBe(false);
    const second = controller.runConversation({ ...request(), resume, sourceNodeId: "turn_1", waitForActiveRun: true });
    expect(oldSignal.aborted).toBe(true);
    await Promise.all([first, second]);
    expect(resume ? streamResume : streamChat).toHaveBeenCalledTimes(resume ? 1 : 2);
    expect(pauseTurn).not.toHaveBeenCalled();
    expect(callbacks.activeRuns.size).toBe(0);
  });

  it("releases an attached paused stream through the real SSE reader abort path without EOF", async () => {
    const actual = await vi.importActual<typeof import("../api")>("../api");
    vi.mocked(streamAttachedTurn).mockImplementationOnce(actual.streamAttachedTurn);
    vi.mocked(streamChat).mockResolvedValueOnce("completed");
    let signal!: AbortSignal;
    vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      signal = init!.signal!;
      return new Response(new ReadableStream<Uint8Array>({
        start(reader) {
          reader.enqueue(new TextEncoder().encode(
            "data: " + JSON.stringify({ type: "turn.snapshot", revision: 0, turn: { ...turn(), status: "paused" } }) + "\n\n",
          ));
          signal.addEventListener("abort", () => reader.error(new DOMException("Aborted", "AbortError")), { once: true });
        },
      }), { status: 200 });
    }));
    const callbacks = controllerCallbacks();
    const controller = createRunController(callbacks);
    const first = controller.runConversation({ ...request(), attach: true });
    await vi.waitFor(() => expect(callbacks.updateConversation).toHaveBeenCalledTimes(1));
    expect(signal.aborted).toBe(false);
    const second = controller.runConversation({ ...request(), waitForActiveRun: true });
    await Promise.all([first, second]);
    expect(signal.aborted).toBe(true);
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(callbacks.recoverConversation).not.toHaveBeenCalled();
    expect(pauseTurn).not.toHaveBeenCalled();
    expect(callbacks.onControlError).not.toHaveBeenCalled();
  });

  it("does not replace a subscription after a running continuation supersedes its terminal Turn", async () => {
    const stream = deferred();
    let signal!: AbortSignal;
    vi.mocked(streamChat).mockImplementationOnce(async (_prompt, onMessage, abortSignal) => {
      signal = abortSignal;
      onMessage({ type: "turn.snapshot", revision: 0, turn: { ...turn(), status: "success" } });
      onMessage({ type: "turn.snapshot", revision: 0, turn: { ...turn(), id: "turn_2", parent_id: "turn_1" } });
      await stream.promise;
      return "completed";
    }).mockResolvedValueOnce("completed");
    const controller = createRunController(controllerCallbacks());
    const first = controller.runConversation(request());
    const second = controller.runConversation({ ...request(), waitForActiveRun: true });
    await Promise.resolve();
    expect(signal.aborted).toBe(false);
    expect(streamChat).toHaveBeenCalledTimes(1);
    stream.resolve();
    await Promise.all([first, second]);
    expect(streamChat).toHaveBeenCalledTimes(2);
  });

  it.each(["refreshSessions", "refreshQueuedMessages"] as const)("releases queued runs without waiting for %s", async (refresh) => {
    const stream = deferred();
    const refreshGate = deferred();
    const callbacks = controllerCallbacks();
    callbacks[refresh].mockReturnValueOnce(refreshGate.promise);
    vi.mocked(streamChat).mockImplementationOnce(async () => {
      await stream.promise;
      return "completed";
    }).mockResolvedValueOnce("completed");
    const controller = createRunController(callbacks);
    const first = controller.runConversation(request());
    const settled = callbacks.activeRuns.get("conversation_1").settled;
    const second = controller.runConversation({ ...request(), waitForActiveRun: true });
    stream.resolve();
    await Promise.all([first, settled, second]);
    expect(streamChat).toHaveBeenCalledTimes(2);
    expect(callbacks.activeRuns.size).toBe(0);
    refreshGate.resolve();
  });

  it("reports refresh failures after releasing the run", async () => {
    const callbacks = controllerCallbacks();
    callbacks.refreshSessions.mockRejectedValueOnce(new Error("sessions unavailable"));
    callbacks.refreshQueuedMessages.mockRejectedValueOnce(new Error("queue unavailable"));
    vi.mocked(streamChat).mockResolvedValueOnce("completed");
    await createRunController(callbacks).runConversation(request());
    await vi.waitFor(() => expect(callbacks.onControlError).toHaveBeenCalledTimes(2));
    expect(callbacks.onControlError).toHaveBeenCalledWith("sessions unavailable");
    expect(callbacks.onControlError).toHaveBeenCalledWith("queue unavailable");
    expect(callbacks.activeRuns.size).toBe(0);
  });

  it("pauses the canonical Turn without a local stream, deduplicates pending requests and permits retry", async () => {
    const pause = deferred();
    vi.mocked(pauseTurn).mockReturnValueOnce(pause.promise);
    const callbacks = controllerCallbacks();
    const controller = createRunController(callbacks);
    controller.stopConversation(turn());
    controller.stopConversation(turn());
    expect(pauseTurn).toHaveBeenCalledTimes(1);
    expect(pauseTurn).toHaveBeenCalledWith("turn_1", "session_1");
    pause.reject(new Error("offline"));
    await vi.waitFor(() => expect(callbacks.onControlError).toHaveBeenCalledWith(expect.stringContaining("offline")));
    controller.stopConversation(turn());
    expect(pauseTurn).toHaveBeenCalledTimes(2);
    controller.stopConversation({ ...turn(), status: "success" });
    expect(pauseTurn).toHaveBeenCalledTimes(2);
  });

  it("uses the current canonical Turn instead of a stale active stream identity", async () => {
    const stream = deferred();
    vi.mocked(streamAttachedTurn).mockImplementationOnce(async () => {
      await stream.promise;
      return "completed";
    });
    const callbacks = controllerCallbacks();
    const controller = createRunController(callbacks);
    const running = controller.runConversation({ ...request(), attach: true });
    controller.stopConversation({ ...turn(), id: "turn_2" });
    expect(pauseTurn).toHaveBeenCalledWith("turn_2", "session_1");
    expect(pauseTurn).toHaveBeenCalledTimes(1);
    stream.resolve();
    await running;
  });



  it.each(["success", "paused", "failed"] as const)("flushes a %s snapshot before transport completion", async (status) => {
    const stream = deferred();
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    vi.mocked(streamChat).mockImplementationOnce(async (_prompt, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: { ...turn(), status } });
      await stream.promise;
      return "completed";
    });
    const callbacks = controllerCallbacks();
    const running = createRunController(callbacks).runConversation(request());
    expect(callbacks.updateConversation).toHaveBeenCalledTimes(1);
    const updater = callbacks.updateConversation.mock.calls[0][1];
    const conversation = updater({ id: "conversation_1", messages: [], runtimeNodes: [] });
    expect(conversation.runtimeNodes[0].status).toBe(status);
    expect(callbacks.activeRuns.has("conversation_1")).toBe(true);
    stream.resolve();
    await running;
  });

  it("projects terminal state immediately while preserving continuation frames until transport ends", async () => {
    const stream = deferred();
    let emit!: Parameters<typeof streamChat>[1];
    let signal!: AbortSignal;
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    vi.mocked(streamChat).mockImplementationOnce(async (_prompt, onMessage, abortSignal) => {
      emit = onMessage;
      signal = abortSignal;
      await stream.promise;
      return "completed";
    });
    const callbacks = controllerCallbacks();
    const controller = createRunController(callbacks);
    const running = controller.runConversation(request());
    emit({ type: "turn.snapshot", revision: 0, turn: turn() });
    expect(callbacks.updateConversation).not.toHaveBeenCalled();
    emit({ type: "turn.delta", session_id: "session_1", turn_id: "turn_1", revision: 1, patch: { status: "success" } });
    expect(callbacks.updateConversation).toHaveBeenCalledTimes(1);
    expect(signal.aborted).toBe(false);
    expect(callbacks.activeRuns.has("conversation_1")).toBe(true);
    const continuation = { ...turn(), id: "turn_2", parent_id: "turn_1" };
    emit({ type: "turn.snapshot", revision: 0, turn: continuation });
    expect(callbacks.activeRuns.get("conversation_1").turnId).toBe("turn_2");
    emit({ type: "turn.delta", session_id: "session_1", turn_id: "turn_2", revision: 1, patch: { status: "success" } });
    expect(callbacks.updateConversation).toHaveBeenCalledTimes(2);
    stream.resolve();
    await running;
    expect(callbacks.activeRuns.size).toBe(0);
  });
});

describe("run controller incremental batching", () => {
  it("stops displaying running after persistence failure without withdrawing visible text", async () => {
    vi.mocked(streamChat).mockImplementationOnce(async (_prompt, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: turn() });
      throw new SseExecutionError("Item persistence failed");
    });
    const updateLastMessage = vi.fn();
    const controller = createRunController({
      activeRuns: new Map(), updateLastMessage,
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      recoverConversation: vi.fn().mockResolvedValue(undefined),
    });
    await controller.runConversation(request());
    const updater = updateLastMessage.mock.calls.at(-1)![1];
    expect(updater({ content: "already visible", running: true, items: [{ type: "text", text: "already visible", status: "running" }] })).toMatchObject({
      content: "already visible", status: "failed", running: false, error: "Item persistence failed",
      items: [{ text: "already visible", status: "failed" }],
    });
  });


  it("silently settles an empty pre-baseline failure and reloads the conversation", async () => {
    vi.mocked(streamChat).mockResolvedValue("silent_failed");
    const recoverConversation = vi.fn().mockResolvedValue(undefined);
    const updateLastMessage = vi.fn();
    const checkSandboxHealth = vi.fn().mockResolvedValue(undefined);
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage,
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation,
      checkSandboxHealth,
    });

    await controller.runConversation(request());

    expect(recoverConversation).toHaveBeenCalledWith("conversation_1", "session_1", "turn_1");
    expect(checkSandboxHealth).not.toHaveBeenCalled();
    const updater = updateLastMessage.mock.calls[0]?.[1] as (message: Record<string, unknown>) => Record<string, unknown>;
    expect(updater({ error: "stale", status: "running", running: true, decision: {} })).toMatchObject({
      error: undefined,
      status: "failed",
      running: false,
      decision: undefined,
    });
  });

  it("clears a model transport error from the final Turn projection", async () => {
    const failed = turn();
    failed.status = "failed";
    failed.data[0][1].content = [{
      type: "error",
      category: "network",
      code: "ModelTransportError",
      message: "Response ended prematurely",
      retryable: false,
      status: "failed",
    }];
    vi.mocked(streamChat).mockImplementation(async (_prompt, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: failed });
      return "completed";
    });
    const updateLastMessage = vi.fn();
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage,
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation: vi.fn().mockResolvedValue(undefined),
    });

    await controller.runConversation(request());

    const updater = updateLastMessage.mock.calls[0]?.[1] as (message: Record<string, unknown>) => Record<string, unknown>;
    expect(updater({ error: "stale", status: "running", running: true })).toMatchObject({
      error: undefined,
      status: "failed",
      running: false,
    });
  });

  it("rechecks sandbox health when a Turn fails before its baseline", async () => {
    vi.mocked(streamChat).mockRejectedValue(new Error("Windows Sandbox Broker unavailable"));
    const checkSandboxHealth = vi.fn().mockResolvedValue(undefined);
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation: vi.fn().mockResolvedValue(undefined),
      checkSandboxHealth,
    });

    await expect(controller.runConversation(request())).rejects.toThrow("Windows Sandbox Broker unavailable");

    expect(checkSandboxHealth).toHaveBeenCalledTimes(1);
  });

  it("does not recheck sandbox health after a Turn baseline was received", async () => {
    vi.mocked(streamChat).mockImplementation(async (_prompt, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: turn() });
      throw new Error("model stream failed");
    });
    const checkSandboxHealth = vi.fn().mockResolvedValue(undefined);
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation: vi.fn().mockResolvedValue(undefined),
      checkSandboxHealth,
    });

    await controller.runConversation(request());

    expect(checkSandboxHealth).not.toHaveBeenCalled();
  });

  it("reports admission rejection without clearing an unaccepted composer draft", async () => {
    vi.mocked(streamChat).mockRejectedValue(new Error("message_queue_unavailable"));
    const onAccepted = vi.fn();
    const onAdmissionRejected = vi.fn();
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation: vi.fn().mockResolvedValue(undefined),
    });

    await expect(controller.runConversation({ ...request(), onAccepted, onAdmissionRejected }))
      .rejects.toThrow("message_queue_unavailable");

    expect(onAccepted).not.toHaveBeenCalled();
    expect(onAdmissionRejected).toHaveBeenCalledTimes(1);
  });

  it("commits multiple SSE frames in one animation-frame state update", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    vi.mocked(streamChat).mockImplementation(async (_prompt, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: turn() });
      onMessage({
        type: "turn.delta",
        session_id: "session_1",
        turn_id: "turn_1",
        revision: 1,
        operations: [{ op: "append_text", data_idx: 0, message_idx: 1, item_idx: 0, delta: "world" }],
      });
      await gate;
      return "completed";
    });

    let animationFrame: FrameRequestCallback | undefined;
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      animationFrame = callback;
      return 1;
    }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());

    let conversation: Conversation = { id: "conversation_1", title: "x", messages: [], runtimeNodes: [] };
    const updateConversation = vi.fn((_id: string, updater: (value: Conversation) => Conversation) => {
      conversation = updater(conversation);
    });
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation,
      recoverConversation: vi.fn().mockResolvedValue(undefined),
    });

    const running = controller.runConversation(request());
    expect(updateConversation).not.toHaveBeenCalled();
    expect(animationFrame).toBeTypeOf("function");
    animationFrame?.(0);
    expect(updateConversation).toHaveBeenCalledTimes(1);
    expect(conversation.messages.map((message) => message.content)).toEqual(["hello", "world"]);
    release();
    await running;
    expect(updateConversation).toHaveBeenCalledTimes(1);
  });

  it("forces a full path projection on the next frame for an SSE current_data_idx patch", async () => {
    let releaseSnapshot!: () => void;
    let releasePatch!: () => void;
    const snapshotGate = new Promise<void>((resolve) => { releaseSnapshot = resolve; });
    const patchGate = new Promise<void>((resolve) => { releasePatch = resolve; });
    vi.mocked(streamChat).mockImplementation(async (_prompt, onMessage) => {
      const versioned = turn();
      versioned.data = [
        [
          { role: "user", content: [{ type: "text", text: "old user", status: "success" }] },
          { role: "assistant", content: [{ type: "text", text: "old answer", status: "success" }] },
        ],
        [
          { role: "user", content: [{ type: "text", text: "new user", status: "success" }] },
          { role: "assistant", content: [{ type: "text", text: "new answer", status: "success" }] },
          { role: "user", delivery_id: "steering-delivery", content: [{ type: "text", text: "new steering", status: "success" }] },
          { role: "assistant", content: [{ type: "text", text: "steering answer", status: "running" }] },
        ],
      ];
      onMessage({ type: "turn.snapshot", revision: 0, turn: versioned });
      await snapshotGate;
      onMessage({
        type: "turn.delta",
        session_id: versioned.session_id,
        turn_id: versioned.id,
        revision: 1,
        patch: { current_data_idx: 1 },
      });
      await patchGate;
      return "completed";
    });

    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());

    let conversation: Conversation = { id: "conversation_1", title: "x", messages: [], runtimeNodes: [] };
    const updateConversation = vi.fn((_id: string, updater: (value: Conversation) => Conversation) => {
      conversation = updater(conversation);
    });
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation,
      recoverConversation: vi.fn().mockResolvedValue(undefined),
    });

    const running = controller.runConversation(request());
    expect(frames).toHaveLength(1);
    frames.shift()?.(0);
    expect(conversation.messages.map((message) => message.content)).toEqual(["old user", "old answer"]);

    releaseSnapshot();
    await vi.waitFor(() => expect(frames).toHaveLength(1));
    frames.shift()?.(1);
    expect(conversation.messages.map((message) => message.content)).toEqual([
      "new user",
      "new answer",
      "new steering",
      "steering answer",
    ]);
    expect(conversation.messages.filter((message) => message.role === "user").map((message) => message.timelineSource))
      .toEqual(["user", "steering"]);

    releasePatch();
    await running;
  });

  it("rebases the same Turn from a repeated authoritative reconnect snapshot", async () => {
    vi.mocked(streamChat).mockImplementation(async (_prompt, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: turn() });
      const rebased = turn();
      rebased.data[0][1].content = [{ type: "text", text: "reconnected", status: "running" }];
      onMessage({ type: "turn.snapshot", revision: 0, turn: rebased });
      onMessage({
        type: "turn.delta",
        session_id: "session_1",
        turn_id: "turn_1",
        revision: 1,
        operations: [{ op: "append_text", data_idx: 0, message_idx: 1, item_idx: 0, delta: " stream" }],
      });
      return "completed";
    });
    let conversation: Conversation = { id: "conversation_1", title: "x", messages: [], runtimeNodes: [] };
    const updateConversation = vi.fn((_id: string, updater: (value: Conversation) => Conversation) => {
      conversation = updater(conversation);
    });
    const recoverConversation = vi.fn().mockResolvedValue(undefined);
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation,
      recoverConversation,
    });

    await controller.runConversation(request());

    expect(recoverConversation).not.toHaveBeenCalled();
    expect(conversation.messages[conversation.messages.length - 1]?.content).toBe("reconnected stream");
  });

  it("pauses and reloads the authoritative session after a protocol error", async () => {
    vi.mocked(streamChat).mockImplementation(async (_prompt, onMessage) => {
      onMessage({
        type: "turn.delta",
        session_id: "session_1",
        turn_id: "turn_1",
        revision: 1,
        operations: [{
          op: "append_item",
          data_idx: 0,
          message_idx: 1,
          item_idx: 0,
          item: { type: "text", text: "bad", status: "running" },
        }],
      });
      return "completed";
    });
    const recoverConversation = vi.fn().mockResolvedValue(undefined);
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation,
    });

    await controller.runConversation(request());

    expect(pauseTurn).toHaveBeenCalledWith("turn_1", "session_1");
    expect(recoverConversation).toHaveBeenCalledWith("conversation_1", "session_1", "turn_1");
  });

  it("attaches to an existing Turn without pausing it", async () => {
    vi.mocked(streamAttachedTurn).mockImplementation(async (_turnId, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: turn() });
      onMessage({
        type: "turn.delta",
        session_id: "session_1",
        turn_id: "turn_1",
        revision: 1,
        patch: { status: "success" },
      });
      return "completed";
    });
    const onBaseline = vi.fn();
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation: vi.fn().mockResolvedValue(undefined),
    });

    await controller.runConversation({ ...request(), attach: true, prompt: null, onBaseline });

    expect(streamAttachedTurn).toHaveBeenCalledWith(
      "turn_1",
      expect.any(Function),
      expect.any(AbortSignal),
      "session_1",
    );
    expect(onBaseline).toHaveBeenCalledTimes(1);
    expect(pauseTurn).not.toHaveBeenCalled();
  });

  it("reloads the final Turn when an attached terminal races the last delta", async () => {
    vi.mocked(streamAttachedTurn).mockImplementation(async (_turnId, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: turn() });
      return "completed";
    });
    const recoverConversation = vi.fn().mockResolvedValue(undefined);
    const updateLastMessage = vi.fn();
    const controller = createRunController({
      activeRuns: new Map(),
      updateLastMessage,
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation,
    });

    await controller.runConversation({ ...request(), attach: true, prompt: null });

    expect(recoverConversation).toHaveBeenCalledWith("conversation_1", "session_1", "turn_1");
    expect(updateLastMessage).not.toHaveBeenCalled();
    expect(pauseTurn).not.toHaveBeenCalled();
  });

  it("waits for the current run to release before starting a queued run", async () => {
    let releaseFirst!: () => void;
    const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
    vi.mocked(streamChat)
      .mockImplementationOnce(async () => {
        await firstGate;
        return "completed";
      })
      .mockResolvedValueOnce("completed");
    const activeRuns = new Map();
    const controller = createRunController({
      activeRuns,
      updateLastMessage: vi.fn(),
      rebindRunSession: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn().mockResolvedValue(undefined),
      updateConversation: vi.fn(),
      recoverConversation: vi.fn().mockResolvedValue(undefined),
    });

    const first = controller.runConversation(request());
    const second = controller.runConversation({
      ...request(),
      turnId: "turn_2",
      prompt: "queued",
      waitForActiveRun: true,
    });
    await Promise.resolve();
    expect(streamChat).toHaveBeenCalledTimes(1);

    releaseFirst();
    await first;
    await second;
    expect(streamChat).toHaveBeenCalledTimes(2);
    expect(vi.mocked(streamChat).mock.calls[1][0]).toBe("queued");
  });
});
