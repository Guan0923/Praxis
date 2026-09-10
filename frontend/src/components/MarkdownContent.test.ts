import { fireEvent, render } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MathJaxBrowserInstance } from "../math/mathjax.d";
import { MATHML_NAMESPACE } from "../math";
import MarkdownContent, { renderMarkdown, renderNativeMathML } from "./MarkdownContent";

const originalMathMLElement = Object.getOwnPropertyDescriptor(window, "MathMLElement");

function enableNativeMathML(): void {
  Object.defineProperty(window, "MathMLElement", {
    configurable: true,
    value: class MathMLElement {},
  });
}

function restoreMathMLElement(): void {
  if (originalMathMLElement) {
    Object.defineProperty(window, "MathMLElement", originalMathMLElement);
  } else {
    delete (window as Window & { MathMLElement?: unknown }).MathMLElement;
  }
}

afterEach(() => {
  restoreMathMLElement();
  document.body.replaceChildren();
});

describe("shared Markdown and LaTeX renderer", () => {
  it("keeps all supported delimiters as complete MathJax source wrappers", () => {
    const html = renderMarkdown(
      "**bold**\n\nInline $x^2$ and \\(\\alpha+1\\).\n\n$$\\frac{a}{b}$$\n\n\\[y_1\\]",
    );
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain('data-latex-source="$x^2$"');
    expect(html).toContain('data-latex-source="\\(\\alpha+1\\)"');
    expect(html).toContain('data-latex-source="$$\\frac{a}{b}$$"');
    expect(html).toContain('data-latex-source="\\[y_1\\]"');
    expect(html.match(/math-source/g)?.length).toBe(4);
  });

  it("supports starred begin/end environments and nested matrix syntax", () => {
    const source = String.raw`\begin{align*}
  A &= \begin{pmatrix}a & b\\ c & d\end{pmatrix}\\
  B &= \ce{H2O}
\end{align*}`;
    const html = renderMarkdown(source);
    const document = new DOMParser().parseFromString(html, "text/html");
    expect(document.querySelector("[data-latex-source]")?.getAttribute("data-latex-source")).toBe(source);
    expect(document.querySelector(".math-display")).not.toBeNull();
  });

  it("keeps currency, code, incomplete math, and unsafe HTML safe", () => {
    const html = renderMarkdown(
      "Cost $12.50 and \\$5; code: `$z_2$`; pending $x\n\n<script>alert(1)</script> [bad](javascript:alert(1))",
    );
    expect(html).toContain("Cost $12.50 and $5");
    expect(html).toContain("<code>$z_2$</code>");
    expect(html).toContain("pending $x");
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("onerror=");
    expect(html).not.toContain('href="javascript:');
    expect(html).not.toContain('data-latex-source="$12.50');
  });

  it("adds safe attributes to external links", () => {
    const html = renderMarkdown("[docs](https://example.com)");
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it("preserves stable paragraphs and formula nodes while the tail grows", () => {
    const text = "Stable $x^2$.\n\nTail";
    const { container, rerender } = render(React.createElement(MarkdownContent, { text, running: true, itemId: "one" }));
    const paragraph = container.querySelector("p");
    const formula = container.querySelector("[data-latex-source]");
    rerender(React.createElement(MarkdownContent, { text: `${text} grows`, running: true, itemId: "one" }));
    expect(container.querySelector("p")).toBe(paragraph);
    expect(container.querySelector("[data-latex-source]")).toBe(formula);
    expect(container.textContent).toContain("Tail grows");
  });

  it("handles math split across fragments, including blank lines", () => {
    const source = "Before\n\n$$x +\n\ny$$\n\nAfter $z$.";
    const { container, rerender } = render(React.createElement(MarkdownContent, { text: "", running: true }));
    for (let end = 1; end <= source.length; end += 1) {
      const text = source.slice(0, end);
      rerender(React.createElement(MarkdownContent, { text, running: true }));
      expect(container.firstElementChild?.innerHTML).toBe(renderMarkdown(text));
    }
  });

  it("updates reference links defined after their paragraphs without replacing unrelated nodes", () => {
    const text = "Stable.\n\n[Docs][target]\n\n";
    const { container, rerender } = render(React.createElement(MarkdownContent, { text, running: true }));
    const stable = container.querySelector("p");
    rerender(React.createElement(MarkdownContent, { text: `${text}[target]: https://example.com\n`, running: true }));
    expect(container.querySelector("a")?.href).toBe("https://example.com/");
    expect(container.querySelector("p")).toBe(stable);
  });

  it("selects the complete formula when the rendered formula is clicked", () => {
    const { container } = render(React.createElement(MarkdownContent, { text: "结果是 $x^2$" }));
    const root = container.firstElementChild as HTMLElement;
    const formula = root.querySelector<HTMLElement>("[data-latex-source]")!;

    fireEvent.click(formula);

    expect(root).toHaveFocus();
    expect(window.getSelection()?.isCollapsed).toBe(false);
    expect(window.getSelection()?.toString()).toContain("$x^2$");
  });

  it("converts every formula atomically to native MathML before committing", async () => {
    enableNativeMathML();
    const root = document.createElement("div");
    root.innerHTML = renderMarkdown("结果是 $x^2$ 和 $$\\frac{a}{b}$$");
    document.body.appendChild(root);
    const mathJax: MathJaxBrowserInstance = {
      tex2mmlPromise: vi.fn(async (body, options) => {
        const display = options?.display ? "block" : "inline";
        return `<math xmlns="${MATHML_NAMESPACE}" display="${display}"><mi>${body}</mi></math>`;
      }),
    };

    const rendered = await renderNativeMathML(root, mathJax, () => true);

    expect(rendered).toBe(true);
    expect(root.querySelectorAll("math")).toHaveLength(2);
    expect(root.querySelectorAll("[data-latex-source]")).toHaveLength(2);
    expect(root.querySelectorAll(".math-native")).toHaveLength(2);
    expect(root.querySelector("[data-latex-source]")?.textContent).toBe("x^2");
    expect(root.querySelector("[data-latex-source]")?.getAttribute("data-latex-source")).toBe("$x^2$");
  });

  it("does not partially commit when MathML conversion fails", async () => {
    enableNativeMathML();
    const root = document.createElement("div");
    root.innerHTML = renderMarkdown("$x$ and $y$");
    document.body.appendChild(root);
    const validMathML = `<math xmlns="${MATHML_NAMESPACE}"><mi>x</mi></math>`;
    const mathJax: MathJaxBrowserInstance = {
      tex2mmlPromise: vi.fn().mockResolvedValueOnce(validMathML).mockRejectedValueOnce(new Error("invalid TeX")),
    };

    const rendered = await renderNativeMathML(root, mathJax, () => true);

    expect(rendered).toBe(false);
    expect(root.querySelector("math")).toBeNull();
    expect(root.textContent).toContain("$x$");
    expect(root.textContent).toContain("$y$");
  });
});
