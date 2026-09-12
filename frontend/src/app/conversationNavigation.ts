import { useEffect, useRef, useState, type SetStateAction } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import type { Conversation, Page } from "../types";

export function chatPath(sessionId: string, threadId: string, agentId?: string) {
  return `/chat/${encodeURIComponent(sessionId)}/${encodeURIComponent(threadId)}`
    + (agentId && agentId !== threadId ? `/agent/${encodeURIComponent(agentId)}` : "");
}

export function parseChatPath(path: string) {
  const match = /^\/chat\/([^/]+)\/([^/]+)(?:\/agent\/([^/]+))?\/?$/.exec(path);
  if (!match) return null;
  try { return { sessionId: decodeURIComponent(match[1]), threadId: decodeURIComponent(match[2]), agentId: match[3] ? decodeURIComponent(match[3]) : undefined }; }
  catch { return null; }
}

export function useConversationNavigation(conversations: Conversation[]) {
  const location = useLocation();
  const navigate = useNavigate();
  const route = parseChatPath(location.pathname);
  const page: Page = location.pathname === "/benchmark" ? "benchmark" : location.pathname === "/trash" ? "trash" : "chat";
  const lastChat = useRef<ReturnType<typeof parseChatPath>>(null);
  if (page === "chat") lastChat.current = route;
  const selected = page === "chat" ? route : lastChat.current;
  const [pendingId, setPendingId] = useState<string | null | undefined>();
  const currentId = pendingId !== undefined ? pendingId : selected?.threadId ?? null;
  const selectionRef = useRef(currentId);
  selectionRef.current = currentId;
  const setCurrentId = (value: SetStateAction<string | null>) => {
    const id = typeof value === "function" ? value(selectionRef.current) : value;
    selectionRef.current = id;
    setPendingId(id);
  };
  useEffect(() => {
    if (pendingId === undefined) return;
    const target = conversations.find((item) => item.id === pendingId);
    if (pendingId !== null && !target?.sessionId) return;
    const path = target?.sessionId ? chatPath(target.sessionId, target.threadId ?? target.id) : "/";
    setPendingId(undefined);
    if (path !== location.pathname) navigate(path);
  }, [pendingId, conversations, location.pathname, navigate]);
  const setPage = (value: SetStateAction<Page>) => {
    const next = typeof value === "function" ? value(page) : value;
    if (next === "chat") {
      if (selectionRef.current !== null) setPendingId(selectionRef.current);
      else navigate("/");
    } else navigate(`/${next}`);
  };
  return { page, currentId, setCurrentId, setPage, route: pendingId === undefined ? route : null, navigate };
}
