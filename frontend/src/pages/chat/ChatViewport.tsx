import { createContext, useContext, useEffect, useLayoutEffect, useMemo, useRef, useSyncExternalStore, type ReactNode, type RefObject } from "react";

type Snapshot = { visible: boolean; height: number };
type Block = {
  element?: HTMLDivElement; snapshot: Snapshot; near: boolean; pinned: boolean;
  listeners: Set<() => void>;
};

// A single observer pair owns all blocks in this conversation.
class ChatViewport {
  private root?: HTMLDivElement;
  private intersection?: IntersectionObserver;
  private resize?: ResizeObserver;
  private blocks = new Map<Element, Block>();
  private width = 0;
  private height = 0;
  private frame = 0;
  readonly enabled = typeof IntersectionObserver !== "undefined" && typeof ResizeObserver !== "undefined";

  private publish(block: Block, visible: boolean, height = block.snapshot.height) {
    if (!visible && block.snapshot.visible && block.element && this.root?.clientHeight) height = block.element.getBoundingClientRect().height;
    if (visible === block.snapshot.visible && height === block.snapshot.height) return;
    block.snapshot = { visible, height };
    for (const listener of block.listeners) listener();
  }

  private protected(block: Block) {
    const element = block.element;
    if (!element) return false;
    if (block.pinned || element.contains(document.activeElement)) return true;
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) return false;
    for (let index = 0; index < selection.rangeCount; index++) {
      if (selection.getRangeAt(index).intersectsNode(element)) return true;
    }
    return false;
  }

  private update = () => {
    this.frame = 0;
    if (!this.root?.clientHeight) return;
    for (const block of this.blocks.values()) this.publish(block, block.near || this.protected(block));
  };
  private schedule = () => {
    if (!this.frame) this.frame = requestAnimationFrame(this.update);
  };

  private observeRange() {
    this.intersection?.disconnect();
    if (!this.root) return;
    this.intersection = new IntersectionObserver((entries) => {
      if (!this.root?.clientHeight) return;
      for (const entry of entries) {
        const block = this.blocks.get(entry.target);
        if (block) block.near = entry.isIntersecting;
      }
      this.update();
    }, { root: this.root, rootMargin: String(this.height * 2) + "px 0px" });
    for (const element of this.blocks.keys()) this.intersection.observe(element);
  }

  connect(root: HTMLDivElement) {
    if (!this.enabled) return () => {};
    this.root = root;
    this.width = root.clientWidth;
    this.height = root.clientHeight;
    this.observeRange();
    this.resize = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.target === root) {
          if (!root.clientHeight || !root.clientWidth) continue;
          if (this.height !== root.clientHeight) {
            this.height = root.clientHeight;
            this.observeRange();
          }
          if (this.width !== root.clientWidth) {
            const ratio = this.width / root.clientWidth;
            this.width = root.clientWidth;
            for (const block of this.blocks.values()) {
              if (!block.snapshot.visible) this.publish(block, false, Math.max(24, block.snapshot.height * ratio));
            }
          }
        } else {
          const block = this.blocks.get(entry.target);
          if (root.clientHeight && block?.snapshot.visible) this.publish(block, true, entry.target.getBoundingClientRect().height);
        }
      }
    });
    this.resize.observe(root);
    for (const element of this.blocks.keys()) this.resize.observe(element);
    root.addEventListener("focusin", this.schedule);
    root.addEventListener("focusout", this.schedule);
    document.addEventListener("selectionchange", this.schedule);
    root.addEventListener("chat-reveal", this.reveal);
    return () => {
      this.intersection?.disconnect();
      this.resize?.disconnect();
      cancelAnimationFrame(this.frame);
      this.frame = 0;
      root.removeEventListener("focusin", this.schedule);
      root.removeEventListener("focusout", this.schedule);
      document.removeEventListener("selectionchange", this.schedule);
      root.removeEventListener("chat-reveal", this.reveal);
      this.root = undefined;
    };
  }

  private reveal = (event: Event) => {
    const target = (event as CustomEvent<HTMLElement>).detail;
    for (const block of this.blocks.values()) {
      if (block.element && target.contains(block.element)) {
        block.near = true;
        this.publish(block, true);
      }
    }
  };

  register(block: Block, element: HTMLDivElement) {
    block.element = element;
    this.blocks.set(element, block);
    this.intersection?.observe(element);
    this.resize?.observe(element);
    if (this.root) {
      const root = this.root.getBoundingClientRect();
      const rect = element.getBoundingClientRect();
      block.near = rect.bottom >= root.top - this.height * 2 && rect.top <= root.bottom + this.height * 2;
      this.publish(block, block.near || this.protected(block));
    }
    return () => {
      this.blocks.delete(element);
      this.intersection?.unobserve(element);
      this.resize?.unobserve(element);
      block.element = undefined;
    };
  }

  pin(block: Block, pinned: boolean) {
    block.pinned = pinned;
    this.publish(block, block.near || this.protected(block));
  }
}

const ViewportContext = createContext<ChatViewport | null>(null);
export function useChatViewport() { return useContext(ViewportContext); }

export function ChatViewportProvider({ scrollRef, children }: { scrollRef: RefObject<HTMLDivElement>; children: ReactNode }) {
  const viewport = useMemo(() => new ChatViewport(), []);
  useEffect(() => {
    if (scrollRef.current) return viewport.connect(scrollRef.current);
  }, [viewport, scrollRef]);
  return <ViewportContext.Provider value={viewport}>{children}</ViewportContext.Provider>;
}

export function VirtualBlock({ id, estimate = 40, pinned = false, revision, children }: {
  id: string; estimate?: number; pinned?: boolean; revision?: unknown; children: ReactNode;
}) {
  const viewport = useChatViewport();
  if (!viewport?.enabled) return <>{children}</>;
  return <ObservedBlock key={id} viewport={viewport} id={id} estimate={estimate} pinned={pinned} revision={revision}>{children}</ObservedBlock>;
}

function ObservedBlock({ viewport, id, estimate, pinned, revision, children }: {
  viewport: ChatViewport; id: string; estimate: number; pinned: boolean; revision?: unknown; children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const block = useMemo<Block>(() => ({ snapshot: { visible: pinned, height: estimate }, near: false, pinned, listeners: new Set() }), []);
  const subscribe = useMemo(() => (listener: () => void) => {
    block.listeners.add(listener);
    return () => { block.listeners.delete(listener); };
  }, [block]);
  const snapshot = useSyncExternalStore(subscribe, () => block.snapshot);
  useLayoutEffect(() => viewport.register(block, ref.current!), [viewport, block]);
  useLayoutEffect(() => viewport.pin(block, pinned), [viewport, block, pinned]);
  useLayoutEffect(() => {
    if (!block.snapshot.visible) {
      block.snapshot = { ...block.snapshot, height: estimate };
      for (const listener of block.listeners) listener();
    }
  }, [block, revision, estimate]);
  return <div ref={ref} data-virtual-block={id} data-mounted={snapshot.visible || pinned ? "true" : "false"}
    style={{ display: "flow-root", minWidth: 0, maxWidth: "100%", overflowAnchor: "none", ...(!snapshot.visible && !pinned ? { height: snapshot.height } : {}) }}>
    {snapshot.visible || pinned ? children : null}
  </div>;
}
