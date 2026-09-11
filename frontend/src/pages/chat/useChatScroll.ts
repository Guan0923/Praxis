import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { ChatMessage } from "../../types";

const BOTTOM_THRESHOLD_PX = 24;

interface ReadingPosition {
  top: number;
  messageId?: string;
  offset: number;
}

function rememberPosition(container: HTMLDivElement): ReadingPosition {
  const top = container.getBoundingClientRect().top + container.clientTop;
  const message = Array.from(container.querySelectorAll<HTMLElement>("[data-scroll-message-id]"))
    .find((element) => element.getBoundingClientRect().bottom > top);
  return {
    top: container.scrollTop,
    messageId: message?.dataset.scrollMessageId,
    offset: message ? message.getBoundingClientRect().top - top : 0,
  };
}

function restorePosition(container: HTMLDivElement, position: ReadingPosition) {
  const message = position.messageId === undefined ? undefined
    : Array.from(container.querySelectorAll<HTMLElement>("[data-scroll-message-id]"))
      .find((element) => element.dataset.scrollMessageId === position.messageId);
  const top = message
    ? container.scrollTop + message.getBoundingClientRect().top
      - container.getBoundingClientRect().top - container.clientTop - position.offset
    : position.top;
  container.scrollTop = Math.max(0, Math.min(top, container.scrollHeight - container.clientHeight));
}

function isAtBottom(scrollContainer: HTMLDivElement): boolean {
  return scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight <= BOTTOM_THRESHOLD_PX;
}

export function useChatScroll(conversationId: string | undefined, messages: ChatMessage[], active = true) {
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);
  const scrollConversationIdRef = useRef<string | undefined>(undefined);
  const [isAtBottomState, setIsAtBottomState] = useState(true);
  const positionRef = useRef<ReadingPosition | null>(null);
  const restoringRef = useRef(false);
  const restoreOnRevealRef = useRef(false);

  const syncBottomState = useCallback((scrollContainer: HTMLDivElement) => {
    if (scrollContainer.clientHeight === 0) return;
    const nextIsAtBottom = isAtBottom(scrollContainer);
    shouldStickToBottomRef.current = nextIsAtBottom;
    positionRef.current = rememberPosition(scrollContainer);
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
      positionRef.current = null;
    }
    if (scrollContainer.clientHeight === 0 || restoringRef.current) return;
    if (!shouldStickToBottomRef.current) {
      if (positionRef.current) restorePosition(scrollContainer, positionRef.current);
      return;
    }
    scrollContainer.scrollTop = scrollContainer.scrollHeight;
    syncBottomState(scrollContainer);
  }, [conversationId, messages, syncBottomState, active]);

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
      } else if (restoringRef.current && positionRef.current) {
        restorePosition(scrollContainer, positionRef.current);
      }
      if (!restoringRef.current) syncBottomState(scrollContainer);
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
    const interruptRestore = () => { restoringRef.current = false; };
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

  const handleScroll = useCallback(() => {
    const scrollContainer = chatScrollRef.current;
    if (active && scrollContainer && !restoringRef.current) syncBottomState(scrollContainer);
  }, [syncBottomState, active]);

  const scrollToBottom = useCallback(() => {
    const scrollContainer = chatScrollRef.current;
    if (!scrollContainer) return;
    restoringRef.current = false;
    scrollContainer.scrollTo({ top: scrollContainer.scrollHeight, behavior: "smooth" });
  }, []);

  return {
    chatScrollRef,
    handleScroll,
    isAtBottom: isAtBottomState,
    scrollToBottom,
  };
}
