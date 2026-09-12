import { act, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../../types";
import { useChatScroll } from "./useChatScroll";

const messages: ChatMessage[] = [{ id: "answer", role: "assistant", content: "Answer", events: [] }];
let frames: Map<number, FrameRequestCallback>;
let nextFrame: number;
let resize: () => void;
const originalResizeObserver = globalThis.ResizeObserver;

function Harness({ active = true, id = "one", content = messages }) {
  const scroll = useChatScroll(id, content, active);
  return <div hidden={!active}>
    <div ref={scroll.chatScrollRef} onScroll={scroll.handleScroll} data-testid="scroll">
      <div className="chat-scroll-content"><div data-scroll-message-id="answer">Answer</div></div>
    </div>
    <output>{String(scroll.isAtBottom)}</output>
    <button onClick={scroll.scrollToBottom}>Bottom</button>
    <button onClick={() => scroll.scrollToPosition(350)}>Timeline</button>
  </div>;
}

function flushFrames() {
  act(() => {
    const pending = [...frames.values()];
    frames.clear();
    pending.forEach((callback) => callback(0));
  });
}

function fixture() {
  const view = render(<Harness />);
  const element = view.getByTestId("scroll");
  const metrics = { top: 350, height: 2000, client: 500, messageTop: 300 };
  Object.defineProperties(element, {
    scrollTo: { configurable: true, value: ({ top }: ScrollToOptions) => { metrics.top = Math.max(0, Math.min(top ?? 0, metrics.height - metrics.client)); } },
    scrollTop: { configurable: true, get: () => metrics.top, set: (top: number) => {
      metrics.top = Math.max(0, Math.min(top, metrics.height - metrics.client));
    } },
    scrollHeight: { configurable: true, get: () => metrics.height },
    clientHeight: { configurable: true, get: () => metrics.client },
  });
  vi.spyOn(element, "getBoundingClientRect").mockImplementation(() => new DOMRect(0, 100, 800, metrics.client));
  const message = element.querySelector<HTMLElement>("[data-scroll-message-id]")!;
  vi.spyOn(message, "getBoundingClientRect").mockImplementation(() =>
    new DOMRect(0, 100 + metrics.messageTop - metrics.top, 800, 900));
  fireEvent.scroll(element);
  flushFrames();
  flushFrames();
  function hide() {
    metrics.top = metrics.height = metrics.client = 0;
    view.rerender(<Harness active={false} />);
    fireEvent.scroll(element);
    act(() => resize());
  }
  function reveal(content = messages, id = "one") {
    metrics.height = 2400;
    metrics.client = 500;
    view.rerender(<Harness content={content} id={id} />);
  }
  return { view, element, metrics, message, hide, reveal };
}

beforeEach(() => {
  frames = new Map();
  nextFrame = 0;
  resize = () => undefined;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    frames.set(++nextFrame, callback);
    return nextFrame;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  globalThis.ResizeObserver = class {
    constructor(callback: ResizeObserverCallback) { resize = () => callback([], this); }
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});
afterEach(() => {
  globalThis.ResizeObserver = originalResizeObserver;
  vi.unstubAllGlobals();
});

describe("retained conversation scrolling", () => {
  it("commits button navigation before streaming and resize updates", () => {
    const f = fixture();
    fireEvent.click(f.view.getByText("Bottom"));
    f.metrics.height = 2400;
    f.view.rerender(<Harness content={[...messages]} />);
    expect(f.metrics.top).toBe(1900);
    fireEvent.click(f.view.getByText("Timeline"));
    f.metrics.height = 2600;
    f.view.rerender(<Harness content={[...messages]} />);
    act(() => resize());
    expect(f.metrics.top).toBe(350);
    expect(f.view.container.querySelector("output")).toHaveTextContent("false");
  });

  it("ignores hidden zero dimensions and restores history before animation frames", () => {
    const f = fixture();
    f.hide();
    f.reveal();
    expect(f.metrics.top).toBe(350);
    expect(f.view.container.querySelector("output")).toHaveTextContent("false");
  });

  it("restores the visible message offset when content above it changes", () => {
    const f = fixture();
    f.hide();
    f.metrics.messageTop = 600;
    f.reveal([...messages]);
    expect(f.metrics.top).toBe(650);
    // A second layout pass must use the original anchor, not its transient position.
    f.metrics.messageTop = 680;
    act(() => resize());
    expect(f.metrics.top).toBe(730);
    flushFrames();
    flushFrames();
    expect(f.metrics.top).toBe(730);
  });

  it("follows new output only when the reader left at the bottom", () => {
    const f = fixture();
    f.metrics.top = 1500;
    fireEvent.scroll(f.element);
    f.hide();
    f.reveal([...messages]);
    expect(f.metrics.top).toBe(1900);
    f.metrics.height = 2600;
    act(() => resize());
    expect(f.metrics.top).toBe(2100);
  });

  it("falls back to the saved offset if the anchor was removed", () => {
    const f = fixture();
    f.hide();
    f.message.removeAttribute("data-scroll-message-id");
    f.reveal();
    expect(f.metrics.top).toBe(350);
  });

  it("clamps restoration when content becomes shorter", () => {
    const f = fixture();
    f.hide();
    f.message.removeAttribute("data-scroll-message-id");
    f.metrics.height = 600;
    f.metrics.client = 500;
    f.view.rerender(<Harness />);
    expect(f.metrics.top).toBe(100);
  });

  it("cancels stale restore callbacks across rapid page switches", () => {
    const f = fixture();
    for (let index = 0; index < 3; index += 1) {
      f.hide();
      f.reveal();
      expect(f.metrics.top).toBe(350);
    }
    flushFrames();
    flushFrames();
    expect(f.metrics.top).toBe(350);
  });

  it("starts a different conversation at its bottom, including while hidden", () => {
    const f = fixture();
    f.hide();
    f.view.rerender(<Harness active={false} id="two" />);
    f.reveal(messages, "two");
    expect(f.metrics.top).toBe(1900);
  });

  it("lets user scrolling interrupt restoration", () => {
    const f = fixture();
    f.hide();
    f.reveal();
    fireEvent.wheel(f.element);
    f.metrics.top = 450;
    fireEvent.scroll(f.element);
    flushFrames();
    flushFrames();
    expect(f.metrics.top).toBe(450);
  });

  it("waits for measurable layout without overwriting the saved reading position", () => {
    const f = fixture();
    f.hide();
    f.view.rerender(<Harness />);
    flushFrames();
    flushFrames();
    expect(f.metrics.top).toBe(0);
    f.metrics.height = 2400;
    f.metrics.client = 500;
    act(() => resize());
    expect(f.metrics.top).toBe(350);
    flushFrames();
    flushFrames();
    expect(f.metrics.top).toBe(350);
  });
});
