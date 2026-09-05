import { useEffect, useSyncExternalStore } from "react";
import {
  claimSessionOwnership,
  sessionOwnership,
  subscribeSessionOwnership,
  type SessionOwnership,
} from "../api/transport";

export function useSessionOwnership(sessionId?: string): SessionOwnership {
  const ownership = useSyncExternalStore(
    subscribeSessionOwnership,
    () => sessionOwnership(sessionId),
    (): SessionOwnership => "unknown",
  );
  useEffect(() => {
    if (sessionId) void claimSessionOwnership(sessionId).catch(() => undefined);
  }, [sessionId]);
  return ownership;
}
