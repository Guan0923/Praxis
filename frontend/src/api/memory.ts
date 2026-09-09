import { jsonBody, requestJson } from "./transport/request";

export interface MemoryConfig {
  enabled: boolean;
  disable_on_external_context: boolean;
  extraction_model: string;
  consolidation_model: string;
  retrieval_limit: number;
  injection_max_items: number;
  injection_max_tokens: number;
  injection_max_bytes: number;
}

export type MemoryKind = "episodic" | "semantic" | "procedural";
export type MemoryStatus = "active" | "disabled" | "superseded" | "deleted";
export type MemoryJobStatus = "pending" | "running" | "succeeded" | "failed" | "cancelled";

export interface MemoryItem {
  memory_id: string;
  kind: MemoryKind;
  title: string;
  content: string;
  summary: string;
  scope: "global" | "project";
  project_id: string | null;
  confidence: number;
  tags: string[];
  status: MemoryStatus;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface MemoryEvidence {
  evidence_id: string;
  memory_id: string;
  session_id: string;
  turn_id: string | null;
  excerpt: string;
  source_kind: string;
  content_sha256: string;
  created_at: string;
}

export interface MemoryJob {
  job_id: string;
  kind: "extract" | "consolidate" | "rebuild_projections";
  status: MemoryJobStatus;
  source_id: string | null;
  project_id: string | null;
  attempts: number;
  max_attempts: number;
  available_at: string;
  lease_expires_at: string | null;
  last_error: string;
  created_at: string;
  updated_at: string;
}

export interface MemoryInjectionRecord {
  session_id?: string;
  recorded_at?: string;
  operation?: string;
  injected?: boolean;
  selected_ids?: string[];
  [key: string]: unknown;
}

export interface MemoryDryRunResult {
  enabled: boolean;
  would_inject: boolean;
  context: string;
  result: Record<string, unknown>;
}

export async function listMemoryItems(): Promise<MemoryItem[]> {
  return (await requestJson<{ items: MemoryItem[] }>("/api/memory/items?include_deleted=true&limit=1000")).items;
}

export async function listMemoryEvidence(memoryId: string): Promise<MemoryEvidence[]> {
  return (await requestJson<{ evidence: MemoryEvidence[] }>(`/api/memory/items/${encodeURIComponent(memoryId)}/evidence?limit=1000`)).evidence;
}

export async function listMemoryJobs(): Promise<MemoryJob[]> {
  return (await requestJson<{ jobs: MemoryJob[] }>("/api/memory/jobs?limit=1000")).jobs;
}

export async function listMemoryInjectionHistory(): Promise<MemoryInjectionRecord[]> {
  return (await requestJson<{ records: MemoryInjectionRecord[] }>("/api/memory/retrieval/history?limit=100")).records;
}

export async function dryRunMemory(query: string, projectId?: string): Promise<MemoryDryRunResult> {
  const params = new URLSearchParams({ query });
  if (projectId) params.set("project_id", projectId);
  return requestJson<MemoryDryRunResult>(`/api/memory/retrieval/dry-run?${params.toString()}`);
}

export async function extractMemory(threadId: string): Promise<MemoryJob> {
  return (await requestJson<{ job: MemoryJob }>("/api/memory/extract", jsonBody({ thread_id: threadId }))).job;
}

export async function consolidateMemory(projectId: string | null = null): Promise<MemoryJob> {
  return (await requestJson<{ job: MemoryJob }>("/api/memory/consolidate", jsonBody({ project_id: projectId }))).job;
}

export async function cancelMemoryJob(jobId: string): Promise<MemoryJob> {
  return (await requestJson<{ job: MemoryJob }>(`/api/memory/jobs/${encodeURIComponent(jobId)}/cancel`, jsonBody({}))).job;
}

export async function setMemoryEnabled(memoryId: string, enabled: boolean): Promise<MemoryItem> {
  return (await requestJson<{ memory: MemoryItem }>(`/api/memory/items/${encodeURIComponent(memoryId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  })).memory;
}

export async function deleteMemory(memoryId: string): Promise<MemoryItem> {
  return (await requestJson<{ memory: MemoryItem }>(`/api/memory/items/${encodeURIComponent(memoryId)}`, { method: "DELETE" })).memory;
}

export async function restoreMemory(memoryId: string): Promise<MemoryItem> {
  return (await requestJson<{ memory: MemoryItem }>(`/api/memory/items/${encodeURIComponent(memoryId)}/restore`, jsonBody({}))).memory;
}

export async function clearMemories(confirm: string): Promise<void> {
  await requestJson("/api/memory/clear", jsonBody({ confirm }));
}
