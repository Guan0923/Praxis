import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ScrollingText from "./ScrollingText";

describe("ScrollingText", () => {
  it("measures overflowing content and updates when space or content changes", () => {
    const { rerender } = render(<ScrollingText text="long-thread-id" focusable />);
    const viewport = screen.getByLabelText("long-thread-id");
    const text = screen.getByText("long-thread-id");
    Object.defineProperty(viewport, "clientWidth", { configurable: true, value: 80 });
    Object.defineProperty(text, "scrollWidth", { configurable: true, value: 240 });
    fireEvent.mouseEnter(viewport);
    expect(viewport).toHaveClass("thread-scroll--overflow");
    expect(viewport.style.getPropertyValue("--thread-scroll-distance")).toBe("-160px");
    Object.defineProperty(viewport, "clientWidth", { configurable: true, value: 300 });
    act(() => window.dispatchEvent(new Event("resize")));
    expect(viewport).not.toHaveClass("thread-scroll--overflow");
    rerender(<ScrollingText text="short" focusable />);
    expect(screen.getByLabelText("short")).not.toHaveClass("thread-scroll--overflow");
  });
});
