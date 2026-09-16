import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { MessageInstance } from "antd/es/message/interface";
import { ErrorAlerts, showErrorMessage } from "./errorFeedback";

describe("error feedback", () => {
  it("uses a bounded duration and a close button for a keyed message", () => {
    const message = { error: vi.fn(), destroy: vi.fn() };
    showErrorMessage(message as unknown as MessageInstance, new Error("failed"));
    const options = message.error.mock.calls[0][0];
    expect(options.duration).toBe(8);
    render(options.content);
    fireEvent.click(screen.getByRole("button", { name: "关闭错误提示" }));
    expect(message.destroy).toHaveBeenCalledWith(options.key);
  });

  it("deduplicates errors and keeps a dismissed polling error closed", async () => {
    const view = render(<ErrorAlerts errors={[new Error("offline"), new Error("offline")]} />);
    expect(screen.getAllByText("offline")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "关闭错误提示" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    view.rerender(<ErrorAlerts errors={[new Error("offline"), new Error("offline")]} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    view.rerender(<ErrorAlerts errors={[new Error("different failure")]} />);
    expect(screen.getByRole("alert")).toHaveTextContent("different failure");
  });
});
