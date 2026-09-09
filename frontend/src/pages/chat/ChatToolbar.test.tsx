import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ChatToolbar } from "./ChatToolbar";
import { ButtonTooltipContext } from "../../components/ButtonTooltipContext";

describe("ChatToolbar", () => {
  it("shows the title and separate thread ID without an ID tooltip", async () => {
    const changeView = vi.fn();
    const props = {
      visible: true,
      currentThreadId: "session-long-thread-id",
      conversationTitle: "Experiment notes with a long conversation title",
      compact: false,
      mainView: "chat" as const,
      onMainViewChange: changeView,
    };
    const { rerender } = render(<ChatToolbar {...props} />);
    expect(screen.getByTitle(props.conversationTitle)).toHaveClass("chat-toolbar-title");
    expect(screen.getByLabelText(props.currentThreadId)).toHaveClass("trace-toolbar-thread-id");
    fireEvent.mouseEnter(screen.getByRole("button", { name: "Thread" }));
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.mouseLeave(screen.getByRole("button", { name: "Thread" }));
    fireEvent.click(screen.getByRole("button", { name: "Trace" }));
    expect(changeView).toHaveBeenCalledWith("trace");

    rerender(<ChatToolbar {...props} compact conversationTitle="Renamed conversation" mainView="trace" />);
    expect(screen.getByTitle("Renamed conversation")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Trace" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Chat" })).toHaveTextContent("");
    fireEvent.mouseEnter(screen.getByRole("button", { name: "Thread" }));
    expect(await screen.findByRole("tooltip")).toHaveTextContent(/^Thread$/);
    fireEvent.click(screen.getByRole("button", { name: "Thread" }));
    expect(await screen.findByRole("menu")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
  });

  it("disables side-chat button hints without removing click menus", async () => {
    render(<ButtonTooltipContext.Provider value={false}><ChatToolbar visible currentThreadId="side-thread" conversationTitle="Side" compact mainView="chat" onMainViewChange={vi.fn()} /></ButtonTooltipContext.Provider>);
    fireEvent.mouseEnter(screen.getByRole("button", { name: "Thread" }));
    fireEvent.click(screen.getByRole("button", { name: "Thread" }));
    expect(await screen.findByRole("menu")).toBeInTheDocument();
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(screen.getByLabelText("side-thread")).toBeInTheDocument();
  });

  it("keeps the title for empty conversations while hiding unavailable thread actions", () => {
    render(<ChatToolbar visible={false} currentThreadId="empty" conversationTitle="新对话" compact={false} mainView="chat" onMainViewChange={vi.fn()} />);
    expect(screen.getByTitle("新对话")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });
});
