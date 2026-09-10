import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { loadMathJax, supportsNativeMathML } from "../math";
import MarkdownContent from "./MarkdownContent";

vi.mock("../math", async () => ({
  ...await vi.importActual<typeof import("../math")>("../math"),
  loadMathJax: vi.fn(),
  supportsNativeMathML: vi.fn(),
}));

afterEach(() => vi.clearAllMocks());

describe("streaming formula lifecycle", () => {
  it("discards a native conversion after its formula was replaced", async () => {
    let resolve!: (value: string) => void;
    const tex2mmlPromise = vi.fn()
      .mockImplementationOnce(() => new Promise<string>((done) => { resolve = done; }))
      .mockResolvedValue('<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>y</mi></math>');
    vi.mocked(supportsNativeMathML).mockReturnValue(true);
    vi.mocked(loadMathJax).mockResolvedValue({ tex2mmlPromise });
    const { container, rerender } = render(<MarkdownContent text="$x$" itemId="one" running />);
    await waitFor(() => expect(tex2mmlPromise).toHaveBeenCalledTimes(1));
    const old = container.querySelector(".math-source")!;
    rerender(<MarkdownContent text="$y$" itemId="one" running />);
    resolve('<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>');
    await waitFor(() => expect(container.querySelector("math")?.textContent).toBe("y"));
    expect(old.isConnected).toBe(false);
    expect(old.querySelector("math")).toBeNull();
  });

  it("clears an obsolete SVG result and preserves the replacement through completion", async () => {
    let resolve!: () => void;
    const typesetClear = vi.fn();
    const typesetPromise = vi.fn()
      .mockImplementationOnce(() => new Promise<void>((done) => { resolve = done; }))
      .mockResolvedValue(undefined);
    vi.mocked(supportsNativeMathML).mockReturnValue(false);
    vi.mocked(loadMathJax).mockResolvedValue({ typesetPromise, typesetClear });
    const { container, rerender } = render(<MarkdownContent text="$x$" itemId="two" running />);
    await waitFor(() => expect(typesetPromise).toHaveBeenCalledTimes(1));
    const old = container.querySelector(".math-source")!;
    rerender(<MarkdownContent text="$y$" itemId="two" running />);
    resolve();
    await waitFor(() => expect(typesetPromise).toHaveBeenCalledTimes(2));
    expect(typesetClear.mock.calls.filter(([nodes]) => nodes?.[0] === old)).toHaveLength(2);
    await waitFor(() => expect(container.querySelector(".math-source")?.getAttribute("data-math-renderer")).toBe("svg"));
    const replacement = container.querySelector(".math-source");
    rerender(<MarkdownContent text="$y$" itemId="two" running={false} />);
    expect(container.querySelector(".math-source")).toBe(replacement);
    expect(typesetPromise).toHaveBeenCalledTimes(2);
  });
});
