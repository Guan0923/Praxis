import { describe, expect, it } from "vitest";
import type { Conversation } from "../types";
import { trimConversationDetails } from "./conversationCache";

function conversation(id: string): Conversation {
  return { id, title: id, messages: [], messagesLoaded: true, runtimeNodes: [] };
}

describe("conversation cache", () => {
  it("keeps every active conversation plus five recently visited idle conversations", () => {
    const values = Array.from({ length: 20 }, (_, index) => conversation(String(index)));
    const active = new Set(values.slice(0, 8).map((item) => item.id));
    const visits = new Map(values.map((item, index) => [item.id, index]));
    const result = trimConversationDetails(values, visits, active);
    expect(result.filter((item) => item.messagesLoaded).map((item) => item.id)).toEqual([
      "0", "1", "2", "3", "4", "5", "6", "7", "15", "16", "17", "18", "19",
    ]);
    expect(result).toHaveLength(20);
    expect(result[10].runtimeNodes).toBeUndefined();
  });
});
