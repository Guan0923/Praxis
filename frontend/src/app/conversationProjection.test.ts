import { describe, expect, it } from "vitest";
import type { Conversation, RuntimeStateNode, RuntimeTreeNode } from "../types";
import { withLoadedTurns, withTurnPage } from "./conversationProjection";
import { TURN_PROTOCOL_VERSION } from "./runtime/runtimeNodeNormalization";

const turn = (
  id: string,
  parentId: string,
  threadId: string,
  prompt: string,
): RuntimeStateNode => ({
  id,
  session_id: "session",
  thread_id: threadId,
  parent_id: parentId,
  parent_session_id: "session",
  parent_thread_id: parentId === "root" ? "session" : threadId,
  version: TURN_PROTOCOL_VERSION,
  firstKeptItemSize: 0,
  compactionId: id,
  user: "",
  provider_name: "provider",
  model: {
    current_model: "model",
    context_length: 8192,
    output_length: 1024,
    thinking: "disable",
    temperature: 0,
    reasoning_effort: "medium",
  },
  permission_mode: "read_only",
  running_mode: "agent",
  usage: {
    input_tokens: null,
    output_tokens: null,
    total_tokens: null,
    cached_tokens: null,
    reasoning_tokens: null,
  },
  cwd: "C:\\work",
  project_cwd: "",
  timestamp: `2026-08-29T00:00:0${id === "anchor" ? 1 : 2}+00:00`,
  status: "success",
  current_data_idx: 0,
  data: [[
    { role: "user", content: [{ type: "text", text: prompt, status: "success" }] },
    { role: "assistant", content: [{ type: "text", text: `${prompt}-answer`, status: "success" }] },
  ]],
});

describe("side-chat conversation projection", () => {
  it("keeps the hidden anchor as context but starts visible Chat at its child Turn", () => {
    const nodes: RuntimeTreeNode[] = [
      { id: "root", session_id: "session", thread_id: "session" },
      turn("anchor", "root", "thread-side", "copied-context"),
      turn("child", "anchor", "thread-side", "visible-question"),
    ];
    const conversation: Conversation = {
      id: "window",
      title: "侧聊 1",
      sessionId: "session",
      threadId: "thread-side",
      hiddenBeforeTurnId: "anchor",
      activeTurnId: "child",
      lastNodeId: "child",
      messages: [],
      messagesLoaded: false,
    };

    const projected = withLoadedTurns(conversation, nodes, "child");

    expect(projected.runtimeNodes).toEqual(nodes);
    expect(projected.messages.map((message) => message.content)).toEqual(["visible-question", "visible-question-answer"]);
    expect(projected.messages.every((message) => message.id.startsWith("child:message:"))).toBe(true);
  });

  it("uses the backend head during a cold hydration", () => {
    const nodes: RuntimeTreeNode[] = [
      { id: "root", session_id: "session", thread_id: "session" },
      turn("anchor", "root", "thread-side", "copied-context"),
      turn("child", "anchor", "thread-side", "persisted-question"),
    ];
    const conversation: Conversation = {
      id: "window",
      title: "侧聊 1",
      sessionId: "session",
      threadId: "thread-side",
      hiddenBeforeTurnId: "anchor",
      messages: [],
      messagesLoaded: false,
    };

    const projected = withTurnPage(conversation, { turns: nodes, current_turn_id: "child", next_cursor: null, has_more: false });

    expect(projected.activeTurnId).toBe("child");
    expect(projected.lastNodeId).toBe("child");
    expect(projected.messages.map((message) => message.content)).toEqual([
      "persisted-question",
      "persisted-question-answer",
    ]);
  });
});


const conversation: Conversation = { id: "session", title: "history", sessionId: "session", threadId: "session", messages: [] };

it("replaces an old cached head with the backend head, ignoring unrelated leaves and timestamps", () => {
  const first = turn("first", "root", "session", "first");
  const second = turn("second", "first", "session", "second");
  const unrelated = { ...turn("unrelated", "root", "session", "wrong"), timestamp: "2099-01-01" };
  const old = withLoadedTurns(conversation, [first, unrelated], first.id);
  const refreshed = withTurnPage(old, { current_turn_id: second.id, turns: [first, second], next_cursor: "server-cursor", has_more: true });
  expect(refreshed.activeTurnId).toBe(second.id);
  expect(refreshed.messages.map((message) => message.content)).toEqual(["first", "first-answer", "second", "second-answer"]);
  expect(refreshed.historyCursor).toBe("server-cursor");
});

it("does not change the displayed head when appending older history", () => {
  const first = turn("first", "root", "session", "first");
  const second = turn("second", "first", "session", "second");
  const current = withTurnPage(conversation, { current_turn_id: second.id, turns: [second], next_cursor: "opaque", has_more: true });
  const appended = withTurnPage(current, { current_turn_id: "newer-server-head", turns: [first], next_cursor: null, has_more: false }, true);
  expect(appended.activeTurnId).toBe(second.id);
  expect(appended.messages[0].content).toBe("first");
  expect(appended.historyCursor).toBeNull();
  expect(appended.historyHasMore).toBe(false);
});

it("retains an undelivered user message across a history refresh", () => {
  const pending = { id: "pending", role: "user" as const, content: "pending", events: [], pending: true, deliveryId: "delivery" };
  const refreshed = withTurnPage({ ...conversation, messages: [pending] }, { current_turn_id: null, turns: [], next_cursor: null, has_more: false });
  expect(refreshed.messages).toEqual([pending]);
});

it("does not guess a head from cached nodes for an empty thread", () => {
  const empty = withTurnPage(conversation, { current_turn_id: null, turns: [], next_cursor: null, has_more: false });
  expect(empty.activeTurnId).toBeUndefined();
  expect(empty.messages).toEqual([]);
});

it("rejects a missing backend head instead of choosing a leaf", () => {
  expect(() => withTurnPage(conversation, { current_turn_id: "missing", turns: [turn("other", "root", "session", "wrong")], next_cursor: null, has_more: false })).toThrow("Current Turn is missing");
});
