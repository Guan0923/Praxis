import type { MarkdownBlock } from "./incrementalMarkdown";

export interface RenderedMarkdownBlock {
  html: string;
  nodes: Node[];
}

function patchNode(current: Node, next: Node, dispose: (node: Node) => void): Node {
  if (current.nodeType !== next.nodeType || current.nodeName !== next.nodeName) {
    dispose(current);
    current.parentNode?.replaceChild(next, current);
    return next;
  }
  if (current.nodeType === Node.TEXT_NODE) {
    if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
    return current;
  }
  if (!(current instanceof Element) || !(next instanceof Element)) return current;
  if (current.hasAttribute("data-latex-source")) {
    if (current.getAttribute("data-latex-source") === next.getAttribute("data-latex-source")
      && current.getAttribute("data-math-display") === next.getAttribute("data-math-display")) return current;
    dispose(current);
    current.parentNode?.replaceChild(next, current);
    return next;
  }
  for (const attr of Array.from(current.attributes)) {
    if (!next.hasAttribute(attr.name)) current.removeAttribute(attr.name);
  }
  for (const attr of Array.from(next.attributes)) {
    if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
  }
  const previousChildren = Array.from(current.childNodes);
  const nextChildren = Array.from(next.childNodes);
  for (let index = 0; index < nextChildren.length; index += 1) {
    if (previousChildren[index]) patchNode(previousChildren[index], nextChildren[index], dispose);
    else current.appendChild(nextChildren[index]);
  }
  for (const extra of previousChildren.slice(nextChildren.length)) {
    dispose(extra);
    extra.parentNode?.removeChild(extra);
  }
  return current;
}

export function reconcileMarkdown(
  root: HTMLElement,
  blocks: MarkdownBlock[],
  previous: Map<number, RenderedMarkdownBlock>,
  dispose: (node: Node) => void,
): Map<number, RenderedMarkdownBlock> {
  const rendered = new Map<number, RenderedMarkdownBlock>();
  for (const block of blocks) {
    const old = previous.get(block.start);
    if (old?.html === block.html) {
      rendered.set(block.start, old);
      continue;
    }
    const template = document.createElement("template");
    template.innerHTML = block.html;
    const fresh = Array.from(template.content.childNodes);
    const nodes = fresh.map((node, index) => old?.nodes[index] ? patchNode(old.nodes[index], node, dispose) : node);
    for (const extra of old?.nodes.slice(fresh.length) ?? []) {
      dispose(extra);
      extra.parentNode?.removeChild(extra);
    }
    rendered.set(block.start, { html: block.html, nodes });
  }
  for (const [key, block] of previous) {
    if (rendered.has(key)) continue;
    for (const node of block.nodes) {
      dispose(node);
      node.parentNode?.removeChild(node);
    }
  }
  let cursor = root.firstChild;
  for (const block of rendered.values()) {
    for (const node of block.nodes) {
      if (node === cursor) cursor = cursor.nextSibling;
      else root.insertBefore(node, cursor);
    }
  }
  return rendered;
}
