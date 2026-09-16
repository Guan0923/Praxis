import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { readEditorFile } from "../api/projects/files";
import PlanFileContent from "./PlanFileContent";
import { notifyFileSaved } from "./rightPanel/fileEvents";

vi.mock("../api/projects/files", () => ({ readEditorFile: vi.fn() }));
vi.mock("./MarkdownContent", () => ({ default: ({ text }: { text: string }) => <div>{text}</div> }));

describe("PlanFileContent", () => {
  it("reads the saved file, opens the scoped file panel, and refreshes after saving", async () => {
    vi.mocked(readEditorFile).mockResolvedValue({ kind: "text", content: "Original file" } as never);
    const opened = vi.fn();
    window.addEventListener("praxis-open-panel-file", opened);
    render(<PlanFileContent sessionId="s1" threadId="t1" path="plan/test.md" />);
    fireEvent.click(await screen.findByText("Original file"));
    expect(screen.getByRole("region", { name: "可编辑计划" })).toHaveTextContent("可实时编辑");
    fireEvent.click(screen.getByRole("button", { name: "编辑计划文件" }));
    expect(opened.mock.calls[0][0].detail).toEqual({ sessionId: "s1", threadId: "t1", source: "workspace", path: "workspace:plan/test.md" });
    vi.mocked(readEditorFile).mockResolvedValue({ kind: "text", content: "Edited file" } as never);
    act(() => notifyFileSaved("s1", "workspace", "workspace:plan/test.md"));
    expect(await screen.findByText("Edited file")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "可编辑计划" })).toHaveClass("is-updated");
    expect(screen.getByText("已同步更新")).toBeInTheDocument();
    expect(screen.queryByText("Original file")).not.toBeInTheDocument();
    window.removeEventListener("praxis-open-panel-file", opened);
  });
});
