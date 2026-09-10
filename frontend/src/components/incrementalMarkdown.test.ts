import MarkdownIt from "markdown-it";
import { describe, expect, it } from "vitest";
import { IncrementalMarkdown } from "./incrementalMarkdown";

const samples = [
  "# Heading\n\nFirst **bold** paragraph.\n\nSecond *italic* paragraph.\n",
  "Heading\n=======\n\n| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\nAfter\n",
  "- first\n  - nested\n\n- loose item\n\nAfter list\n",
  "> Quote\n> continued\n>\n> - list\n\nOutside\n",
  "Before\n\n```js\nconst value = '[ref]';\n\nconsole.log(value);\n```\n\nAfter\n",
  "Visit [docs][ref].\n\nAnother paragraph.\n\n[ref]: https://example.com \"Title\"\n\nEnd\n",
  "[ref]: https://first.example\n\n[link][ref]\n\n[ref]: https://second.example\n\nEnd\n",
  "中文段落\r\n\r\n下一段，包含 **加粗**。\r\n",
];

describe("incremental Markdown parsing", () => {
  it.each(samples)("matches full parsing across arbitrary fragment boundaries: %s", (source) => {
    const markdown = new MarkdownIt({ html: false, breaks: true });
    const parser = new IncrementalMarkdown(markdown);
    for (let end = 1; end <= source.length; end += 1) {
      const prefix = source.slice(0, end);
      expect(parser.update(prefix, true).map((block) => block.html).join("")).toBe(markdown.render(prefix));
    }
    expect(parser.update(source, false).map((block) => block.html).join("")).toBe(markdown.render(source));
  });

  it("does not parse stable paragraphs again during an ordinary append", () => {
    const parser = new IncrementalMarkdown(new MarkdownIt());
    const prefix = "Stable paragraph.\n\n".repeat(100);
    const before = parser.update(`${prefix}Tail`, true);
    const parsed = parser.parsedCharacters;
    const after = parser.update(`${prefix}Tail grows`, true);
    expect(after[0]).toBe(before[0]);
    expect(parser.parsedCharacters - parsed).toBe("Tail grows".length);
  });

  it("rebuilds only when content is replaced or a stream is sealed", () => {
    const markdown = new MarkdownIt();
    const parser = new IncrementalMarkdown(markdown);
    parser.update("Old\n\nTail", true);
    expect(parser.update("Replacement", true).map((block) => block.html).join("")).toBe(markdown.render("Replacement"));
    expect(parser.update("Replacement", false).map((block) => block.html).join("")).toBe(markdown.render("Replacement"));
  });
});
