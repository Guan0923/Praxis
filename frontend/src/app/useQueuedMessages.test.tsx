import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { listQueuedMessages } from "../api";
import type { Conversation } from "../types";
import type { QueuedMessage } from "./types";
import { useQueuedMessages } from "./useQueuedMessages";

vi.mock("../api", () => ({ listQueuedMessages: vi.fn() }));

const current: Conversation = { id: "conversation", threadId: "thread", title: "test", messages: [] };
const item: QueuedMessage = { id: "local", thread_id: "thread", content: "unsaved", references: [], state: "pending", created_at: "2026-09-09", updated_at: "2026-09-09" };

describe("queue refresh ordering", () => {
  it("preserves submitting and failed local messages across refreshes", async () => {
    vi.mocked(listQueuedMessages).mockResolvedValue([]);
    const { result } = renderHook(() => useQueuedMessages({ current, conversations: [current], panelConversations: {}, onError: vi.fn() }));
    await act(async () => {});
    act(() => result.current.updateQueuedMessages(current.id, () => [{ ...item, saving: true }]));
    await act(async () => result.current.refreshQueuedMessages(current.id));
    expect(result.current.queuedMessages.get(current.id)).toEqual([{ ...item, saving: true }]);
    act(() => result.current.updateQueuedMessages(current.id, () => [{ ...item, error: "offline" }]));
    await act(async () => result.current.refreshQueuedMessages(current.id));
    expect(result.current.queuedMessages.get(current.id)?.[0].error).toBe("offline");
  });

  it("ignores an older GET that completes after a newer GET", async () => {
    let older!: (items: QueuedMessage[]) => void;
    vi.mocked(listQueuedMessages).mockImplementationOnce(() => new Promise((resolve) => { older = resolve; })).mockResolvedValue([item]);
    const { result } = renderHook(() => useQueuedMessages({ current, conversations: [current], panelConversations: {}, onError: vi.fn() }));
    await act(async () => result.current.refreshQueuedMessages(current.id));
    await act(async () => older([]));
    await waitFor(() => expect(result.current.queuedMessages.get(current.id)).toEqual([item]));
  });
});
