import { beforeEach, describe, expect, it, vi } from "vitest";
import { createConversationActions } from "./conversationActions";
import { deletedConversations, pendingConversationDeletes } from "./conversationDeletion";
import { deleteSession } from "../api";

vi.mock("../api", () => ({ deleteSession: vi.fn(), archiveSession: vi.fn(), forkTurn: vi.fn(), getSessionNodes: vi.fn(), listSessions: vi.fn(), renameSession: vi.fn(), restoreSession: vi.fn() }));
vi.mock("./conversationDeletion", () => ({ deletedConversations: new Set(), pendingConversationDeletes: new Set(), waitForDeletion: vi.fn().mockResolvedValue(undefined) }));

function context(saved = true) {
  return {
    conversations: [{ id: "conversation", title: "test", messages: [], ...(saved ? { sessionId: "session", threadId: "branch" } : {}) }],
    activeConversations: [], currentId: "conversation", ensureSession: vi.fn(), updateConversation: vi.fn(),
    setConversations: vi.fn(), setCurrentId: vi.fn(), setPage: vi.fn(), setActionError: vi.fn(),
  };
}

beforeEach(() => { vi.clearAllMocks(); deletedConversations.clear(); pendingConversationDeletes.clear(); });

describe("conversation deletion", () => {
  it("deletes an unsaved draft without creating a session", async () => {
    const state = context(false);
    await createConversationActions(state).deleteConversation("conversation");
    expect(state.ensureSession).not.toHaveBeenCalled();
    expect(deleteSession).not.toHaveBeenCalled();
    expect(deletedConversations.has("conversation")).toBe(true);
  });
  it("sends exact owner ids and prevents duplicate requests until deletion succeeds", async () => {
    let finish!: () => void;
    vi.mocked(deleteSession).mockReturnValue(new Promise<void>((resolve) => { finish = resolve; }));
    const state = context();
    const actions = createConversationActions(state);
    const pending = actions.deleteConversation("conversation");
    await actions.deleteConversation("conversation");
    expect(deleteSession).toHaveBeenCalledTimes(1);
    expect(deleteSession).toHaveBeenCalledWith("branch", "session");
    expect(state.setConversations).not.toHaveBeenCalled();
    finish(); await pending;
    expect(state.setConversations).toHaveBeenCalledTimes(1);
    expect(deletedConversations.has("conversation")).toBe(true);
  });
  it("preserves the conversation and allows retry after failure", async () => {
    vi.mocked(deleteSession).mockRejectedValue(new Error("SQLite busy"));
    const state = context();
    await expect(createConversationActions(state).deleteConversation("conversation")).rejects.toThrow("SQLite busy");
    expect(state.setConversations).not.toHaveBeenCalled();
    expect(deletedConversations.size).toBe(0);
    expect(pendingConversationDeletes.size).toBe(0);
  });
});
