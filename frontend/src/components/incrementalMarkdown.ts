import type { Env, MarkdownIt, Token } from "markdown-it";

export interface MarkdownBlock {
  start: number;
  source: string;
  html: string;
  tokens: Token[];
}

const instrumented = new WeakSet<MarkdownIt>();

function referencesChanged(before: Env, after: Env): boolean {
  const left = before.references ?? {};
  const right = after.references ?? {};
  return Object.keys(left).length !== Object.keys(right).length
    || Object.keys(right).some((key) => left[key]?.href !== right[key].href || left[key]?.title !== right[key].title);
}

function mayExtendMath(block: MarkdownBlock): boolean {
  if (["fence", "code_block", "math_block", "math_block_eqno"].includes(block.tokens[0]?.type)) return false;
  // Unresolved math may become a single block after a later closing delimiter.
  const unresolvedDollar = block.tokens.some((token) => token.children?.some(
    (child) => child.type === "text" && /\$(?!\d)/u.test(child.content),
  ));
  const source = block.source;
  return unresolvedDollar
    || (source.includes("\\[") && !source.includes("\\]"))
    || (source.includes("\\(") && !source.includes("\\)"))
    || (source.match(/\\begin\{/gu)?.length ?? 0) > (source.match(/\\end\{/gu)?.length ?? 0);
}

export class IncrementalMarkdown {
  private source = "";
  private blocks: MarkdownBlock[] = [];
  private stableOffset = 0;
  private stableEnv: Env = {};
  private lastEnv: Env = {};
  private running = false;
  parsedCharacters = 0;

  constructor(private readonly markdown: MarkdownIt) {
    if (!instrumented.has(markdown)) {
      markdown.core.ruler.before("strip_references", "incremental_reference_ranges", (state) => {
        state.env.incrementalDefinitions = state.tokens.filter((token) => token.type === "reference_definition");
      });
      instrumented.add(markdown);
    }
  }

  update(raw: string, running: boolean): MarkdownBlock[] {
    const source = raw.replace(/\r\n?/gu, "\n").replace(/\0/gu, "\uFFFD");
    if (source === this.source && running === this.running) return this.blocks;
    const append = source.startsWith(this.source) && !(this.running && !running);
    const offset = append ? this.stableOffset : 0;
    const env: Env = { references: { ...(append ? this.stableEnv.references : {}) } };
    const tail = source.slice(offset);
    this.parsedCharacters += tail.length;
    const tokens = this.markdown.parse(tail, env);
    const definitions = (env.incrementalDefinitions ?? []) as Token[];
    const lines = [0];
    for (let index = 0; index < tail.length; index += 1) {
      if (tail[index] === "\n") lines.push(index + 1);
    }
    const groups: { start: number; tokens: Token[] }[] = [];
    for (const token of tokens) {
      if (token.level === 0 && token.nesting !== -1 && token.map) {
        groups.push({ start: offset + (lines[token.map[0]] ?? tail.length), tokens: [] });
      }
      groups[groups.length - 1]?.tokens.push(token);
    }
    let prefix = append ? this.blocks.filter((block) => block.start < offset) : [];
    if (offset && referencesChanged(this.lastEnv, env)) {
      prefix = prefix.map((block) => {
        if (!block.source.includes("[") || ["fence", "code_block"].includes(block.tokens[0]?.type)) return block;
        this.parsedCharacters += block.source.length;
        const html = this.markdown.render(block.source, env);
        return html === block.html ? block : { ...block, html };
      });
    }
    const changed = groups.map((group, index): MarkdownBlock => ({
      start: group.start,
      source: source.slice(group.start, groups[index + 1]?.start ?? source.length),
      html: this.markdown.renderer.render(group.tokens, this.markdown.options, env),
      tokens: group.tokens,
    }));
    const blocks = [...prefix, ...changed];
    let stableOffset = changed[changed.length - 1]?.start ?? offset;
    for (const block of changed) {
      if (mayExtendMath(block)) {
        stableOffset = Math.min(stableOffset, block.start);
        break;
      }
    }
    const stableEnv: Env = { references: { ...(append ? this.stableEnv.references : {}) } };
    for (const token of definitions) {
      if (!token.map || offset + (lines[token.map[0]] ?? tail.length) >= stableOffset) continue;
      const label = token.meta?.label;
      if (typeof label === "string" && env.references?.[label]) stableEnv.references![label] = env.references[label];
    }
    this.source = source;
    this.blocks = blocks;
    this.stableOffset = stableOffset;
    this.stableEnv = stableEnv;
    this.lastEnv = env;
    this.running = running;
    return blocks;
  }
}
