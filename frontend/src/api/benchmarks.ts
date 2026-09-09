import type { BenchmarkRun, BenchmarkRuns, BenchmarkTraceEvent, SkillInfo, TaskInfo, ToolInfo } from "../types";
import { jsonBody, requestJson } from "./transport/request";

export async function listTasks(): Promise<TaskInfo[]> {
  return requestJson<TaskInfo[]>("/benchmark/tasks");
}

export async function runBenchmark(task: string, planner: string): Promise<BenchmarkRun> {
  return requestJson<BenchmarkRun>("/benchmark/run", {
    ...jsonBody({ task, planner }), operation: { dedupeKey: `benchmark:start:${task}` },
  });
}

export async function runAllBenchmark(planner: string): Promise<BenchmarkRun> {
  return requestJson<BenchmarkRun>("/benchmark/run-all", jsonBody({ planner }));
}

export function listBenchmarkRuns(signal?: AbortSignal): Promise<BenchmarkRuns> {
  return requestJson("/benchmark/runs", { signal });
}

export function getBenchmarkTrace(runId: string, taskId: string, signal?: AbortSignal): Promise<BenchmarkTraceEvent[]> {
  return requestJson(`/benchmark/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/trace`, { signal });
}

export function cancelBenchmark(runId: string, taskId?: string): Promise<BenchmarkRun> {
  const taskPath = taskId ? `/tasks/${encodeURIComponent(taskId)}` : "";
  return requestJson(`/benchmark/runs/${encodeURIComponent(runId)}${taskPath}/cancel`, jsonBody({}));
}

export async function listTools(): Promise<ToolInfo[]> {
  return requestJson<ToolInfo[]>("/api/tools");
}

export async function listSkills(): Promise<SkillInfo[]> {
  return requestJson<SkillInfo[]>("/api/skills");
}
