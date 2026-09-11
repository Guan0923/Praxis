const CAPACITY = 1024 * 1024;
const HALF = CAPACITY / 2;
const encoder = new TextEncoder();
const decoder = new TextDecoder();

export class TerminalOutputBuffer {
  private head = new Uint8Array(0);
  private tail = new Uint8Array(0);
  total = 0;

  append(text: string): void {
    const bytes = encoder.encode(text);
    const start = this.total;
    this.total += bytes.length;
    const headSize = Math.max(0, Math.min(HALF - start, bytes.length));
    const head = new Uint8Array(this.head.length + headSize);
    head.set(this.head);
    head.set(bytes.subarray(0, headSize), this.head.length);
    this.head = head;
    const remainder = bytes.subarray(headSize);
    const size = Math.min(HALF, this.tail.length + remainder.length);
    const tail = new Uint8Array(size);
    const keep = Math.max(0, size - remainder.length);
    tail.set(this.tail.subarray(this.tail.length - keep), 0);
    tail.set(remainder.subarray(Math.max(0, remainder.length - size)), keep);
    this.tail = tail;
  }

  omit(bytes: number): void {
    if (!Number.isSafeInteger(bytes) || bytes <= 0) return;
    this.total += bytes;
    this.tail = new Uint8Array(0);
  }

  get retainedBytes(): number { return this.head.length + this.tail.length; }

  snapshot(): string {
    if (this.total <= CAPACITY) {
      const bytes = new Uint8Array(this.retainedBytes);
      bytes.set(this.head);
      bytes.set(this.tail, this.head.length);
      return decoder.decode(bytes);
    }
    let end = this.head.length;
    let characterStart = end - 1;
    while (characterStart > 0 && (this.head[characterStart] & 0xc0) === 0x80) characterStart--;
    const lead = this.head[characterStart];
    const width = lead >= 0xf0 ? 4 : lead >= 0xe0 ? 3 : lead >= 0xc0 ? 2 : 1;
    if (end - characterStart < width) end = characterStart;
    let start = 0;
    while (start < this.tail.length && (this.tail[start] & 0xc0) === 0x80) start++;
    const omitted = this.total - end - (this.tail.length - start);
    return decoder.decode(this.head.subarray(0, end)) + `\r\n[... ${omitted} bytes omitted ...]\r\n`
      + decoder.decode(this.tail.subarray(start));
  }
}
