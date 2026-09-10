import { useEffect, useRef, useState } from "react";
import { cancelBenchmark, listBenchmarkRuns, runAllBenchmark, runBenchmark, listBenchmarkResources, changeBenchmarkResources, type BenchmarkResource } from "../../api";
import type { BenchmarkRun, BenchmarkStatus } from "../../types";

const INSTANCE_KEY = "praxis:benchmark-instance";

export function isActive(status: BenchmarkStatus): boolean {
  return status === "queued" || status === "running" || status === "stopping";
}

function rememberedInstance(): string | null {
  try { return sessionStorage.getItem(INSTANCE_KEY); } catch { return null; }
}

export function useBenchmarkRuns(active = true) {
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  const [runs, setRuns] = useState<BenchmarkRun[]>([]);
  const [resources, setResources] = useState<BenchmarkResource[]>([]);
  const [connectionError, setConnectionError] = useState<Error | string | null>(null);
  const [actionError, setActionError] = useState<Error | string | null>(null);
  const [expired, setExpired] = useState(false);
  const [ready, setReady] = useState(false);
  const [pending, setPending] = useState<Set<string>>(new Set());
  const instance = useRef(rememberedInstance());
  const revision = useRef(0);
  const mounted = useRef(false);
  const pendingRef = useRef(new Set<string>());

  function acceptInstance(value: string) {
    const changed = instance.current !== null && instance.current !== value;
    if (changed) setExpired(true);
    instance.current = value;
    try { sessionStorage.setItem(INSTANCE_KEY, value); } catch { /* Storage may be disabled. */ }
    return changed;
  }

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const synchronize = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", synchronize);
    return () => document.removeEventListener("visibilitychange", synchronize);
  }, []);

  useEffect(() => {
    if (!active || !visible) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | undefined;
    async function poll() {
      const startedRevision = revision.current;
      controller = new AbortController();
      const requestController = controller;
      let timedOut = false;
      const timeout = setTimeout(() => {
        timedOut = true;
        requestController.abort();
      }, 10_000);
      try {
        const [data, resourceData] = await Promise.all([
          listBenchmarkRuns(requestController.signal), listBenchmarkResources(requestController.signal),
        ]);
        if (stopped || startedRevision !== revision.current) return;
        acceptInstance(data.instance_id);
        setRuns(data.runs);
        setResources(resourceData);
        setConnectionError(null);
        setReady(true);
      } catch (error) {
        if (!stopped && startedRevision === revision.current) {
          setConnectionError(timedOut ? "状态查询超时，正在重新连接。" : error instanceof Error ? error : String(error));
        }
      } finally {
        clearTimeout(timeout);
        if (!stopped) timer = setTimeout(poll, 1000);
      }
    }
    void poll();
    return () => {
      stopped = true;
      controller?.abort();
      clearTimeout(timer);
    };
  }, [active, visible]);

  async function resourceAction(name: string, operation: "prepare" | "delete") {
    const key = `resources:${name}`;
    if (pendingRef.current.has(key)) return;
    pendingRef.current.add(key);
    setPending(new Set(pendingRef.current));
    setActionError(null);
    try {
      const resource = await changeBenchmarkResources(name, operation);
      if (!mounted.current) return;
      revision.current += 1;
      setResources((previous) => [...previous.filter((item) => item.task_name !== name), resource]);
    } catch (error) {
      if (mounted.current) setActionError(error instanceof Error ? error : String(error));
    } finally {
      pendingRef.current.delete(key);
      if (mounted.current) setPending(new Set(pendingRef.current));
    }
  }

  async function action(key: string, request: () => Promise<BenchmarkRun>) {
    if (pendingRef.current.has(key)) return;
    pendingRef.current.add(key);
    setPending(new Set(pendingRef.current));
    setActionError(null);
    try {
      const run = await request();
      if (!mounted.current) return;
      revision.current += 1;
      const changed = acceptInstance(run.instance_id);
      setRuns((previous) => [...(changed ? [] : previous.filter((item) => item.id !== run.id)), run]);
    } catch (error) {
      if (mounted.current) setActionError(error instanceof Error ? error : String(error));
    } finally {
      pendingRef.current.delete(key);
      if (mounted.current) setPending(new Set(pendingRef.current));
    }
  }

  return {
    runs, resources, resourceAction, connectionError, actionError, expired, ready, pending,
    start: (name?: string) => action(name ?? "all", () => name ? runBenchmark(name, "llm") : runAllBenchmark("llm")),
    stop: (runId: string, taskId?: string) => action(`stop:${taskId ?? runId}`, () => cancelBenchmark(runId, taskId)),
  };
}
