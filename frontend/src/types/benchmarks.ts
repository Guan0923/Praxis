import type { TurnTraceContext, TurnTraceItem } from "./runtime";

export interface TaskInfo {
  name: string;
  capability: string;
  description: string;
  difficulty: string;
  prompt: string;
  budgets: {
    max_tool_calls: number | null;
    timeout_seconds: number;
  };
  suite_version: string;
  environment: { kind: "docker" | "local"; status: string };
  tags: string[];
  source: {
    benchmark: string;
    task_id: string;
    url: string;
    source_revision: string;
    license: string;
    adaptation_notes: string;
  };
  planner_modes: string[];
}

export type BenchmarkTraceRecord = {
  session_id: string;
  thread_id: string;
  turn_id: string;
  data_idx: number;
} & (
  | { type: "context"; data: TurnTraceContext | null }
  | { type: "item"; data: TurnTraceItem }
);

export interface BenchmarkResult {
  task_name: string;
  capability?: string;
  status?: string;
  score?: number | null;
  final_answer?: string;
  metrics?: Record<string, unknown>;
  verdicts?: Array<Record<string, unknown>>;
  error?: string | null;
  run_id?: string | null;
  passed?: boolean;
  attempt?: number;
  trace?: BenchmarkTraceRecord[];
  failure_phase?: string | null;
}

export type BenchmarkStatus = "queued" | "running" | "stopping" | "completed" | "failed" | "cancelled";

export interface BenchmarkTaskRun {
  id: string;
  task_name: string;
  status: BenchmarkStatus;
  phase: string;
  activity: string;
  updated_at: string;
  duration_ms: number;
  result: BenchmarkResult | null;
  trace_count: number;
}

export interface BenchmarkRun {
  id: string;
  instance_id: string;
  created_at: string;
  status: BenchmarkStatus;
  total: number;
  finished: number;
  tasks: BenchmarkTaskRun[];
}

export interface BenchmarkRuns {
  instance_id: string;
  runs: BenchmarkRun[];
}
