import { act, cleanup, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatViewportProvider, VirtualBlock } from "./ChatViewport";
import MarkdownContent from "../../components/MarkdownContent";

let intersection: IntersectionObserverCallback;
let observed: Set<Element>;
let disconnected: number;

beforeEach(() => {
  observed = new Set();
  disconnected = 0;
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(600);
  vi.stubGlobal("IntersectionObserver", class {
    constructor(callback: IntersectionObserverCallback) { intersection = callback; }
    observe(element: Element) { observed.add(element); }
    unobserve(element: Element) { observed.delete(element); }
    disconnect() { observed.clear(); disconnected++; }
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function intersect(element: Element, visible: boolean) {
  act(() => intersection([{ target: element, isIntersecting: visible } as IntersectionObserverEntry], {} as IntersectionObserver));
}

function Harness({ pinned = false, revision = 1 }: { pinned?: boolean; revision?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  return <div ref={ref}><ChatViewportProvider scrollRef={ref}>
    <VirtualBlock id="history" estimate={120} revision={revision}><p>Historical body</p></VirtualBlock>
    <VirtualBlock id="active" pinned={pinned}><input aria-label="active input" /></VirtualBlock>
  </ChatViewportProvider></div>;
}

describe("chat viewport", () => {
  it("starts with placeholders, measures before unloading and releases registrations", () => {
    const view = render(<Harness />);
    const block = view.container.querySelector('[data-virtual-block="history"]')!;
    expect(screen.queryByText("Historical body")).toBeNull();
    expect(observed.size).toBe(2);
    intersect(block, true);
    expect(screen.getByText("Historical body")).toBeTruthy();
    vi.spyOn(block, "getBoundingClientRect").mockReturnValue({ height: 333 } as DOMRect);
    intersect(block, false);
    expect(screen.queryByText("Historical body")).toBeNull();
    expect((block as HTMLElement).style.height).toBe("333px");
    intersect(block, true);
    expect(screen.getByText("Historical body")).toBeTruthy();
    view.unmount();
    expect(observed.size).toBe(0);
    expect(disconnected).toBeGreaterThan(0);
  });

  it("retains active and focused blocks and unloads them after protection ends", () => {
    const view = render(<Harness pinned />);
    const block = view.container.querySelector('[data-virtual-block="active"]')!;
    intersect(block, false);
    const input = screen.getByRole("textbox", { name: "active input" });
    input.focus();
    view.rerender(<Harness />);
    intersect(block, false);
    expect(screen.getByRole("textbox")).toBe(input);
    input.blur();
    intersect(block, false);
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("invalidates an offscreen measurement when its content changes", () => {
    const view = render(<Harness />);
    const block = view.container.querySelector('[data-virtual-block="history"]')!;
    intersect(block, true);
    vi.spyOn(block, "getBoundingClientRect").mockReturnValue({ height: 333 } as DOMRect);
    intersect(block, false);
    view.rerender(<Harness revision={2} />);
    expect((block as HTMLElement).style.height).toBe("120px");
  });

  it("retains selected content until the selection is cleared", () => {
    const view = render(<Harness />);
    const block = view.container.querySelector('[data-virtual-block="history"]')!;
    intersect(block, true);
    const range = document.createRange();
    range.selectNodeContents(screen.getByText("Historical body"));
    window.getSelection()!.addRange(range);
    intersect(block, false);
    expect(screen.getByText("Historical body")).toBeTruthy();
    window.getSelection()!.removeAllRanges();
    intersect(block, false);
    expect(screen.queryByText("Historical body")).toBeNull();
  });

  it("registers Markdown paragraphs without mounting all historical DOM", () => {
    function MarkdownHarness() {
      const ref = useRef<HTMLDivElement>(null);
      return <div ref={ref}><ChatViewportProvider scrollRef={ref}>
        <MarkdownContent itemId="long" text={Array.from({ length: 100 }, (_, index) => "Paragraph " + index).join("\n\n")} />
      </ChatViewportProvider></div>;
    }
    const view = render(<MarkdownHarness />);
    expect(view.container.querySelectorAll("[data-virtual-block]").length).toBe(100);
    expect(view.container.querySelectorAll("p").length).toBe(0);
    const first = view.container.querySelector("[data-virtual-block]")!;
    intersect(first, true);
    expect(view.container.querySelectorAll("p").length).toBe(1);
    intersect(first, false);
    expect(view.container.querySelectorAll("p").length).toBe(0);
  });
});
