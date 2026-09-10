import type { LocalProfile } from "../types";
import type { MemoryConfig } from "./memory";
import { requestJson, requestOptionalJson, requestVoid } from "./transport/request";

export type UserProfile = LocalProfile;

export type AppearanceMode = "light" | "dark";

export interface AppearanceConfig {
  mode: AppearanceMode;
}

export interface AgentConfig {
  tone: string;
  verbosity: string;
  initiative: string;
  custom_instructions: string;
  display_mode: "minimal" | "medium" | "verbose" | "developer";
  timezone: string;
  location_enabled: boolean;
}

export interface ProviderConfig {
  id: string;
  is_active: boolean;
  provider_name: string;
  protocol: "chat_completions" | "responses" | "messages";
  base_url: string;
  model: string;
  max_tokens: number;
  context_size: number;
  temperature: number;
  tokenizer_model: string;
  api_key_configured: boolean;
}

export interface TimezoneOption {
  identifier: string;
  label: string;
}

export type TerminalType = "cmd" | "git_bash" | "powershell" | "pwsh" | "wsl";

export interface RuntimeConfig {
  max_tool_calls: number;
  max_tool_parellel: number;
  terminal_type: TerminalType;
}

export type SandboxNetworkMode = "no_network" | "restricted_network" | "full_network";

export interface SandboxLimits {
  wall_seconds: number;
  cpu_seconds: number;
  memory_mib: number;
  processes: number;
  handles: number;
  output_chars: number;
  write_io_mib: number;
}

export interface SandboxNetworkRule {
  host: string;
  port?: number;
}

export interface SandboxConfig {
  network_mode: SandboxNetworkMode;
  network_allowlist: SandboxNetworkRule[];
  readonly proxy_port: number;
  limits: SandboxLimits;
}

export interface TerminalOption {
  value: TerminalType;
  label: string;
}

export interface UserSettings {
  appearance_config: AppearanceConfig;
  profile: UserProfile;
  agent_config: AgentConfig;
  provider_config: ProviderConfig;
  provider_configs: ProviderConfig[];
  capability_config: Record<string, unknown>;
  runtime_config: RuntimeConfig;
  sandbox_config: SandboxConfig;
  terminal_options: TerminalOption[];
  terminal_notice: string | null;
  timezone_options: TimezoneOption[];
  memory_config: MemoryConfig;
}

export interface SkillSettingsItem {
  directory: string;
  name: string;
  description: string;
  metadata: Record<string, string>;
  allowed_tools: string[];
  root: string;
  enabled: boolean;
}

export interface SkillSettingsResponse {
  enabled: boolean;
  skills: SkillSettingsItem[];
}

export interface McpSecretStatus {
  name: string;
  configured: boolean;
}

export interface McpServerSettings {
  transport: "stdio" | "streamable_http";
  url: string | null;
  headers: Record<string, string>;
  secret_headers: McpSecretStatus[];
  name: string;
  command: string;
  args: string[];
  cwd: string | null;
  env: Record<string, string>;
  secret_env: McpSecretStatus[];
  enabled: boolean;
}

export interface McpSettingsResponse {
  enabled: boolean;
  servers: McpServerSettings[];
}

export interface McpServerInput {
  transport: "stdio" | "streamable_http";
  url?: string | null;
  headers?: Record<string, string>;
  header_secrets?: Record<string, string>;
  remove_header_secrets?: string[];
  command: string;
  args: string[];
  cwd: string | null;
  env: Record<string, string>;
  secrets: Record<string, string>;
  remove_secrets: string[];
  enabled: boolean;
}

export function getSettings(): Promise<UserSettings> {
  return requestJson<UserSettings>("/api/settings");
}

export function updateAppearanceConfig(mode: AppearanceMode): Promise<AppearanceConfig> {
  return requestJson<AppearanceConfig>("/api/settings/appearance", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
}

export function getSkillSettings(): Promise<SkillSettingsResponse> {
  return requestJson<SkillSettingsResponse>("/api/settings/skills");
}

export function setSkillsEnabled(enabled: boolean): Promise<SkillSettingsResponse> {
  return requestJson<SkillSettingsResponse>("/api/settings/skills/enabled", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export function setSkillEnabled(directory: string, enabled: boolean): Promise<SkillSettingsResponse> {
  return requestJson<SkillSettingsResponse>(`/api/settings/skills/${encodeURIComponent(directory)}/enabled`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export function importSkill(): Promise<{ directory: string } | null> {
  return requestOptionalJson<{ directory: string }>("/api/settings/skills/import", { method: "POST" });
}

export function deleteSkill(directory: string): Promise<void> {
  return requestVoid(`/api/settings/skills/${encodeURIComponent(directory)}`, { method: "DELETE" });
}

export function getMcpSettings(): Promise<McpSettingsResponse> {
  return requestJson<McpSettingsResponse>("/api/settings/mcp");
}

export function setMcpEnabled(enabled: boolean): Promise<McpSettingsResponse> {
  return requestJson<McpSettingsResponse>("/api/settings/mcp/enabled", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export function createMcpServer(values: McpServerInput & { name: string }): Promise<McpServerSettings> {
  return requestJson<McpServerSettings>("/api/settings/mcp/servers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

export function updateMcpServer(name: string, values: McpServerInput): Promise<McpServerSettings> {
  return requestJson<McpServerSettings>(`/api/settings/mcp/servers/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

export function setMcpServerEnabled(name: string, enabled: boolean): Promise<McpServerSettings> {
  return requestJson<McpServerSettings>(`/api/settings/mcp/servers/${encodeURIComponent(name)}/enabled`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export function deleteMcpServer(name: string): Promise<void> {
  return requestVoid(`/api/settings/mcp/servers/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export interface McpConnectionTest {
  tools: string[];
  count: number;
  protocol_version: string;
  capabilities: string[];
  counts: { tools: number; resources: number; resource_templates: number; prompts: number };
}

export function testMcpServer(name: string): Promise<McpConnectionTest> {
  return requestJson<McpConnectionTest>(
    `/api/settings/mcp/servers/${encodeURIComponent(name)}/test`,
    { method: "POST" },
  );
}

export function getProfile(): Promise<UserProfile> {
  return requestJson<UserProfile>("/api/settings/profile");
}

export function updateProfile(profile: UserProfile): Promise<UserProfile> {
  return requestJson<UserProfile>("/api/settings/profile", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profile),
  });
}

export function updateAgentConfig(config: AgentConfig): Promise<AgentConfig> {
  return requestJson<AgentConfig>("/api/settings/agent", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export function updateRuntimeConfig(config: RuntimeConfig): Promise<RuntimeConfig> {
  return requestJson<RuntimeConfig>("/api/settings/runtime", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export function updateMemoryConfig(config: MemoryConfig): Promise<MemoryConfig> {
  return requestJson<MemoryConfig>("/api/settings/memory", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export function updateSandboxConfig(config: SandboxConfig): Promise<SandboxConfig> {
  return requestJson<SandboxConfig>("/api/settings/sandbox", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export interface SandboxBrokerStatus {
  error_report?: import("./errorReport").ErrorReport | null;
  installed: boolean;
  healthy: boolean;
  code?: string | null;
  version?: string | null;
  installation_id?: string | null;
  detail?: string | null;
}

export function getSandboxStatus(): Promise<SandboxBrokerStatus> {
  return requestJson<SandboxBrokerStatus>("/api/sandbox/status");
}

export function repairSandboxBroker(): Promise<SandboxBrokerStatus> {
  return requestJson<SandboxBrokerStatus>("/api/sandbox/repair", { method: "POST" });
}



type ProviderInput = Omit<ProviderConfig, "id" | "is_active" | "api_key_configured"> & { api_key?: string };

export function updateProviderConfig(config: ProviderInput): Promise<ProviderConfig> {
  return requestJson<ProviderConfig>("/api/settings/providers", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export function addProviderConfig(config: ProviderInput): Promise<ProviderConfig> {
  return requestJson<ProviderConfig>("/api/settings/providers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export function updateProviderConfigById(
  id: string,
  values: {
    provider_name?: string;
    model?: string;
    max_tokens?: number;
    context_size?: number;
    temperature?: number;
    api_key?: string;
  },
): Promise<ProviderConfig> {
  return requestJson<ProviderConfig>(`/api/settings/providers/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

export function activateProviderConfig(id: string): Promise<ProviderConfig> {
  return requestJson<ProviderConfig>(`/api/settings/providers/${encodeURIComponent(id)}/active`, {
    method: "PUT",
  });
}

export function deleteProviderConfig(id: string): Promise<ProviderConfig[]> {
  return requestJson<ProviderConfig[]>(`/api/settings/providers/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

const modelDiscoveries = new Map<string, Promise<{ models: string[] }>>();

export function discoverProviderModels(values: {
  config_id?: string;
  provider_name: string;
  protocol: ProviderConfig["protocol"];
  base_url: string;
  api_key?: string;
}): Promise<{ models: string[] }> {
  const body = JSON.stringify({ config_id: values.config_id, provider_name: values.provider_name,
    protocol: values.protocol, base_url: values.base_url, api_key: values.api_key });
  const existing = modelDiscoveries.get(body);
  if (existing) return existing;
  const request = requestJson<{ models: string[] }>("/api/settings/providers/models", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    operation: { dedupeKey: `models:${crypto.randomUUID()}` },
  });
  modelDiscoveries.set(body, request);
  const cleanup = () => { if (modelDiscoveries.get(body) === request) modelDiscoveries.delete(body); };
  void request.then(cleanup, cleanup);
  return request;
}
