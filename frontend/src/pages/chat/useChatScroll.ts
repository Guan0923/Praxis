import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { ChatMessage } from "../../types";
import { patchView, useViewState, viewSnapshot } from "../../app/viewState";

const BOTTOM_THRESHOLD_PX = 24;

interface ReadingPosition {
  top: number;
  messageId?: string;
  offset: number;
  blockId?: string;
  blockOffset?: number;
}

// Keep precise block anchors only while the corresponding loaded projection exists.
const readingPositions = new WeakMap<ChatMessage[], ReadingPosition>();

function rememberPosition(container: HTMLDivElement, previous: ReadingPosition | null): ReadingPosition {
  const top = container.getBoundingClientRect().top + container.clientTop;
  let message = previous?.messageId
    ? container.querySelector<HTMLElement>('[data-scroll-message-id="' + CSS.escape(previous.messageId) + '"]') : null;
  if (!message || message.getBoundingClientRect().bottom <= top || message.getBoundingClientRect().top > top) {
    const messages = container.querySelectorAll<HTMLElement>("[data-scroll-message-id]");
    let left = 0;
    let right = messages.length;
    while (left < right) {
      const middle = (left + right) >>> 1;
      if (messages[middle].getBoundingClientRect().bottom <= top) left = middle + 1;
      else right = middle;
    }
    message = messages[left] ?? null;
  }
  const blocks = message?.querySelectorAll<HTMLElement>("[data-virtual-block]");
  let block: HTMLElement | undefined;
  if (blocks?.length) {
    let left = 0;
    let right = blocks.length;
    while (left < right) {
      const middle = (left + right) >>> 1;
      if (blocks[middle].getBoundingClientRect().top <= top) left = middle + 1;
      else right = middle;
    }
    block = blocks[Math.max(0, left - 1)];
  }
  return {
    top: container.scrollTop,
    messageId: message?.dataset.scrollMessageId,
    offset: message ? message.getBoundingClientRect().top - top : 0,
    blockId: block?.dataset.virtualBlock,
    blockOffset: block ? block.getBoundingClientRect().top - top : undefined,
  };
}

function restorePosition(container: HTMLDivElement, position: ReadingPosition) {
  const block = position.blockId ? container.querySelector<HTMLElement>('[data-virtual-block="' + CSS.escape(position.blockId) + '"]') : null;
  const message = block ?? (position.messageId === undefined ? undefined
    : container.querySelector<HTMLElement>('[data-scroll-message-id="' + CSS.escape(position.messageId) + '"]'));
  const top = message
    ? container.scrollTop + message.getBoundingClientRect().top
      - container.getBoundingClientRect().top - container.clientTop - (block ? position.blockOffset ?? 0 : position.offset)
    : position.top;
  container.scrollTop = Math.max(0, Math.min(top, container.scrollHeight - container.clientHeight));
}

function isAtBottom(scrollContainer: HTMLDivElement): boolean {
  return scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight <= BOTTOM_THRESHOLD_PX;
}

export function useChatScroll(conversationId: string | undefined, messages: ChatMessage[], active = true,
  persistence?: { key: string; hasMore?: boolean; loadEarlier: () => Promise<void> }) {
  const saved = useViewState(persistence?.key ?? "", () => null);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);
  const scrollConversationIdRef = useRef<string | undefined>(undefined);
  const [isAtBottomState, setIsAtBottomState] = useState(true);
  const positionRef = useRef<ReadingPosition | null>(null);
  const restoringRef = useRef(false);
  const restoreOnRevealRef = useRef(false);
  const loadedKey = useRef<string | undefined>(undefined);
  const interrupted = useRef(false);
  const persistenceRef = useRef(persistence);
  persistenceRef.current = persistence;

  const syncBottomState = useCallback((scrollContainer: HTMLDivElement) => {
    if (scrollContainer.clientHeight === 0) return;
    const nextIsAtBottom = isAtBottom(scrollContainer);
    shouldStickToBottomRef.current = nextIsAtBottom;
    positionRef.current = rememberPosition(scrollContainer, positionRef.current);
    readingPositions.set(messagesRef.current, positionRef.current);
    setIsAtBottomState((current) => current === nextIsAtBottom ? current : nextIsAtBottom);
  }, []);

  useLayoutEffect(() => {
    if (!active) return;
    const scrollContainer = chatScrollRef.current;
    if (!scrollContainer) return;
    const conversationChanged = scrollConversationIdRef.current !== conversationId;
    scrollConversationIdRef.current = conversationId;
    if (conversationChanged) {
      shouldStickToBottomRef.current = true;
      setIsAtBottomState(true);
      positionRef.current = null;
      loadedKey.current = undefined;
      interrupted.current = false;
    }
    if (persistence?.key && !saved.loaded) return;
    if (persistence?.key && loadedKey.current !== persistence.key) {
      loadedKey.current = persistence.key;
      const reading = viewSnapshot(persistence.key).reading;
      if (reading) {
        const cached = readingPositions.get(messages);
        positionRef.current = cached && cached.messageId === reading.messageId && cached.top === reading.top && cached.offset === reading.offset
          ? cached : { ...reading, messageId: reading.messageId ?? undefined };
        shouldStickToBottomRef.current = reading.atBottom;
      }
    }
    if (!interrupted.current && positionRef.current?.messageId && !shouldStickToBottomRef.current
      && !messages.some((item) => item.id === positionRef.current?.messageId) && persistence?.hasMore) {
      void persistence.loadEarlier();
      return;
    }
    if (scrollContainer.clientHeight === 0 || restoringRef.current) return;
    if (!shouldStickToBottomRef.current) {
      if (positionRef.current) restorePosition(scrollContainer, positionRef.current);
      return;
    }
    scrollContainer.scrollTop = scrollContainer.scrollHeight;
    syncBottomState(scrollContainer);
  }, [conversationId, messages, syncBottomState, active, saved.loaded, persistence?.key, persistence?.hasMore]);

  useLayoutEffect(() => {
    if (!active) {
      restoreOnRevealRef.current = true;
      return;
    }
    const scrollContainer = chatScrollRef.current;
    const scrollContent = scrollContainer?.querySelector<HTMLElement>(".chat-scroll-content");
    if (!scrollContainer || !scrollContent) return;
    let disposed = false;
    let frame: number | undefined;
    restoringRef.current = restoreOnRevealRef.current;
    restoreOnRevealRef.current = false;
    const restore = () => {
      if (disposed || scrollContainer.clientHeight === 0) return;
      if (shouldStickToBottomRef.current) {
        scrollContainer.scrollTop = scrollContainer.scrollHeight;
      } else if (positionRef.current) {
        restorePosition(scrollContainer, positionRef.current);
      }
      if (!restoringRef.current && !persistenceRef.current?.key) syncBottomState(scrollContainer);
      else if (frame === undefined) {
        // Keep the anchor through the first resize delivery after revealing Splitter.
        frame = requestAnimationFrame(() => {
          restore();
          frame = requestAnimationFrame(() => {
            restore();
            restoringRef.current = false;
            if (!disposed) syncBottomState(scrollContainer);
          });
        });
      }
    };
    restore();
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(restore) : null;
    observer?.observe(scrollContainer);
    observer?.observe(scrollContent);
    const interruptRestore = () => { restoringRef.current = false; interrupted.current = true; };
    scrollContainer.addEventListener("wheel", interruptRestore, { passive: true });
    scrollContainer.addEventListener("pointerdown", interruptRestore);
    scrollContainer.addEventListener("keydown", interruptRestore);
    return () => {
      disposed = true;
      if (frame !== undefined) cancelAnimationFrame(frame);
      observer?.disconnect();
      restoringRef.current = false;
      scrollContainer.removeEventListener("wheel", interruptRestore);
      scrollContainer.removeEventListener("pointerdown", interruptRestore);
      scrollContainer.removeEventListener("keydown", interruptRestore);
    };
  }, [conversationId, syncBottomState, active]);

  const scrollFrame = useRef<number>();
  useLayoutEffect(() => () => {
    if (scrollFrame.current !== undefined) cancelAnimationFrame(scrollFrame.current);
    scrollFrame.current = undefined;
  }, [conversationId, active]);
  const handleScroll = useCallback(() => {
    const scrollContainer = chatScrollRef.current;
    if (active && scrollContainer && !restoringRef.current) {
      if (!interrupted.current && positionRef.current?.messageId && persistenceRef.current?.hasMore
        && !scrollContainer.querySelector(`[data-scroll-message-id="${CSS.escape(positionRef.current.messageId)}"]`)) return;
      const atBottom = isAtBottom(scrollContainer);
      const previous = positionRef.current;
      if (previous) {
        const delta = scrollContainer.scrollTop - previous.top;
        positionRef.current = { ...previous, top: scrollContainer.scrollTop,
          offset: previous.offset - delta,
          blockOffset: previous.blockOffset === undefined ? undefined : previous.blockOffset - delta };
      }
      shouldStickToBottomRef.current = atBottom;
      setIsAtBottomState((current) => current === atBottom ? current : atBottom);
      if (scrollFrame.current !== undefined) return;
      scrollFrame.current = requestAnimationFrame(() => {
        scrollFrame.current = undefined;
        syncBottomState(scrollContainer);
        const position = positionRef.current;
        if (persistenceRef.current?.key && position) patchView(persistenceRef.current.key,
          { reading: { top: position.top, messageId: position.messageId, offset: position.offset, atBottom: shouldStickToBottomRef.current } }, 1000);
      });
    }
  }, [syncBottomState, active]);

  const scrollToPosition = useCallback((top: number, behavior: ScrollBehavior = "instant") => {
    const scrollContainer = chatScrollRef.current;
    if (!scrollContainer) return;
    restoringRef.current = false;
    interrupted.current = true;
    // Smooth scrolling updates the reading position through scroll events.
    scrollContainer.scrollTo({ top, behavior });
    syncBottomState(scrollContainer);
    handleScroll();
  }, [syncBottomState, handleScroll]);

  const scrollToBottom = useCallback(() => {
    const container = chatScrollRef.current;
    if (container) scrollToPosition(container.scrollHeight, "smooth");
  }, [scrollToPosition]);

  return {
    chatScrollRef,
    handleScroll,
    isAtBottom: isAtBottomState,
    scrollToBottom,
    scrollToPosition,
  };
}
