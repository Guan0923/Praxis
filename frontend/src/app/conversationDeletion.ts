import { ApiError, requestJson } from "../api/transport/request";
import type { ErrorReport } from "../api/errorReport";

export const deletedConversations = new Set<string>();
export const pendingConversationDeletes = new Set<string>();

export async function waitForDeletion(threadId: string): Promise<void> {
  for (;;) {
    const result = await requestJson<{ status: string; error_report?: ErrorReport }>(
      "/api/sidebar-threads/" + encodeURIComponent(threadId) + "/deletion",
    );
    if (result.status === "failed") throw new ApiError(500, "对话已删除，但资源清理失败", undefined, result.error_report);
    if (result.status === "completed") return;
    if (result.status === "unavailable") throw new Error("对话已删除，但后端未保留本次清理结果。");
    await new Promise<void>((resolve) => setTimeout(resolve, 500));
  }
}
