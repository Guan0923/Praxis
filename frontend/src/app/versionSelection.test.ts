import { describe, expect, it, vi } from "vitest";
import { displayVersion, receiveVersion, selectVersion } from "./versionSelection";
import { patchTurnCurrentData, type VersionSelection } from "../api/conversations/turns";
import type { Conversation, RuntimeStateNode } from "../types";
import { withLoadedTurns } from "./conversationProjection";

vi.mock("../api/conversations/turns", () => ({ patchTurnCurrentData: vi.fn() }));

function fixture(id: string): Conversation {
  const node = { id, session_id: id, thread_id: id, parent_id: "", parent_session_id: id,
    version: "1", status: "success", current_data_idx: 0, data: [0, 1, 2].map((version) => [
      { role: "user", content: [{ type: "text", text: `question ${version}`, status: "success" }] },
      { role: "assistant", content: [{ type: "text", text: `answer ${version}`, status: "success" }] },
    ]) } as RuntimeStateNode;
  return withLoadedTurns({ id, sessionId: id, threadId: id, title: id, messages: [] }, [node]);
}

describe("version selection", () => {
  it("shows versions before saving and coalesces rapid clicks", async () => {
    let current = fixture("rapid");
    let finish!: (value: VersionSelection) => void;
    vi.mocked(patchTurnCurrentData).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    vi.mocked(patchTurnCurrentData).mockImplementationOnce(async (id, index) => ({ id, session_id: id, thread_id: id, current_data_idx: index, revision: 2 }));
    const update = (_: string, fn: (value: Conversation) => Conversation) => { current = fn(current); };
    const saving = selectVersion(current, "rapid", 1, update, vi.fn());
    expect(current.messages[0].content).toBe("question 1");
    await selectVersion(current, "rapid", 1, update, vi.fn());
    expect(current.messages[0].content).toBe("question 2");
    finish({ id: "rapid", session_id: "rapid", thread_id: "rapid", current_data_idx: 1, revision: 1 });
    await saving;
    expect(current.messages[0].content).toBe("question 2");
    expect(patchTurnCurrentData).toHaveBeenLastCalledWith("rapid", 2, "rapid");
  });

  it("restores the confirmed version on failure", async () => {
    let current = fixture("failure");
    vi.mocked(patchTurnCurrentData).mockRejectedValueOnce(new Error("offline"));
    const error = vi.fn();
    await selectVersion(current, "failure", 1, (_, fn) => { current = fn(current); }, error);
    expect(current.messages[0].content).toBe("question 0");
    expect(error).toHaveBeenCalledOnce();
  });

  it("preserves unrelated message identities", () => {
    const current = fixture("identity");
    const other = { id: "unrelated", role: "user" as const, content: "keep", events: [] };
    current.messages.push(other);
    const selected = displayVersion(current, "identity", 1);
    expect(selected.messages.at(-1)).toBe(other);
  });

  it("does not let an old failure overwrite a newer selection", async () => {
    let current = fixture("newer");
    let fail!: (error: Error) => void;
    vi.mocked(patchTurnCurrentData).mockImplementationOnce(() => new Promise((_, reject) => { fail = reject; }));
    vi.mocked(patchTurnCurrentData).mockImplementationOnce(async (id, index) => ({ id, session_id: id, thread_id: id, current_data_idx: index, revision: 2 }));
    const update = (_: string, fn: (value: Conversation) => Conversation) => { current = fn(current); };
    const saving = selectVersion(current, "newer", 1, update, vi.fn());
    await selectVersion(current, "newer", 1, update, vi.fn());
    fail(new Error("old request failed"));
    await saving;
    expect(current.messages[0].content).toBe("question 2");
  });

  it("ignores older confirmations while retaining new Turn data", async () => {
    let current = fixture("events");
    let finish!: (value: VersionSelection) => void;
    vi.mocked(patchTurnCurrentData).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const update = (_: string, fn: (value: Conversation) => Conversation) => { current = fn(current); };
    const saving = selectVersion(current, "events", 1, update, vi.fn());
    current.runtimeNodes = current.runtimeNodes!.map((node) => ({ ...node, updated_at: "new backend data" }));
    receiveVersion({ id: "events", session_id: "events", thread_id: "events", current_data_idx: 2, revision: 3 }, update, current.id);
    expect(current.messages[0].content).toBe("question 1");
    finish({ id: "events", session_id: "events", thread_id: "events", current_data_idx: 1, revision: 2 });
    await saving;
    expect(current.messages[0].content).toBe("question 2");
    expect(current.runtimeNodes![0]).toMatchObject({ updated_at: "new backend data" });
  });
});
