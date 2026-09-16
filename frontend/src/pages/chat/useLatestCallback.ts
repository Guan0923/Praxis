import { useCallback, useLayoutEffect, useRef } from "react";

export function useLatestCallback<Args extends unknown[], Result>(callback: (...args: Args) => Result) {
  const ref = useRef(callback);
  useLayoutEffect(() => { ref.current = callback; });
  return useCallback((...args: Args) => ref.current(...args), []);
}
