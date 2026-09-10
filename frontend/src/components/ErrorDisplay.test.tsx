import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/transport/request";
import { ErrorDisplay } from "./ErrorDisplay";

describe("ErrorDisplay", () => {
  it("shows the original error and expands plain-text frames", async () => {
    const report = { type: "OSError", message: "[Errno 22] Invalid argument", traceback: 'File "pipe.py", line 44, in read\n<script>bad()</script>' };
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<ErrorDisplay error={new ApiError(503, "wrapped", "broker_pipe_unavailable", report)} />);
    expect(screen.getByText("OSError: [Errno 22] Invalid argument")).toBeInTheDocument();
    fireEvent.click(screen.getByText("错误详情"));
    expect(await screen.findByText(/File "pipe.py"/)).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "复制错误详情" }));
    expect(writeText).toHaveBeenCalledWith(report.traceback);
  });
  it("does not invent a stack for a business message", () => {
    render(<ErrorDisplay error="项目名称不能为空" />);
    expect(screen.getByText("项目名称不能为空")).toBeInTheDocument();
    expect(screen.queryByText("错误详情")).not.toBeInTheDocument();
  });
});
