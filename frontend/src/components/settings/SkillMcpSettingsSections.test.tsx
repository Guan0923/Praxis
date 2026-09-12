import { App as AntApp } from "antd";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { McpSettingsSection } from "./McpSettingsSection";
import { SkillSettingsSection } from "./SkillSettingsSection";

const api = vi.hoisted(() => ({
  getSkillSettings: vi.fn(),
  setSkillsEnabled: vi.fn(),
  setSkillEnabled: vi.fn(),
  importSkill: vi.fn(),
  deleteSkill: vi.fn(),
  getMcpSettings: vi.fn(),
  setMcpEnabled: vi.fn(),
  saveMcpSettings: vi.fn(),
  testMcpServer: vi.fn(),
}));

vi.mock("../../api", () => api);

const skillSettings = {
  enabled: true,
  skills: [{
    directory: "demo-folder",
    name: "demo",
    description: "Demo workflow.",
    metadata: { owner: "local" },
    allowed_tools: ["read_file"],
    root: "C:/Users/demo/.praxis/skills/demo-folder",
    enabled: true,
  }],
};

const mcpSettings = { enabled: false, mcpServers: {
  trace: { type: "stdio", command: "python", args: ["server.py"], env: { TOKEN: "<stored-secret>" } },
} };

function renderWithApp(node: React.ReactNode) {
  return render(<AntApp>{node}</AntApp>);
}

describe("SkillSettingsSection", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.clearAllMocks();
    api.getSkillSettings.mockResolvedValue(structuredClone(skillSettings));
    api.setSkillsEnabled.mockResolvedValue(structuredClone(skillSettings));
    api.setSkillEnabled.mockResolvedValue(structuredClone(skillSettings));
    api.importSkill.mockResolvedValue({ directory: "imported" });
    api.deleteSkill.mockResolvedValue(undefined);
  });

  it("loads metadata on demand and rolls a failed row switch back", async () => {
    api.setSkillEnabled.mockRejectedValueOnce(new Error("保存失败"));
    renderWithApp(<SkillSettingsSection />);

    expect(await screen.findByText("Demo workflow.")).toBeInTheDocument();
    expect(screen.getByText("owner: local")).toBeInTheDocument();
    expect(screen.getByText("read_file")).toBeInTheDocument();
    const toggle = screen.getByRole("switch", { name: "启用 Skill demo" });
    expect(toggle).toBeChecked();

    await userEvent.click(toggle);
    await screen.findByText("保存失败");
    expect(toggle).toBeChecked();
  });

  it("refreshes after import and confirms permanent deletion", async () => {
    api.getSkillSettings
      .mockResolvedValueOnce(structuredClone(skillSettings))
      .mockResolvedValueOnce({ enabled: true, skills: [] })
      .mockResolvedValueOnce(structuredClone(skillSettings));
    renderWithApp(<SkillSettingsSection />);
    await screen.findByText("Demo workflow.");

    await userEvent.click(screen.getByRole("button", { name: /导入 Skill/ }));
    await waitFor(() => expect(api.importSkill).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.getSkillSettings).toHaveBeenCalledTimes(2));

    cleanup();
    renderWithApp(<SkillSettingsSection />);
    await screen.findByText("Demo workflow.");
    await userEvent.click(screen.getByRole("button", { name: "删除 Skill demo" }));
    await userEvent.click(await screen.findByRole("button", { name: "永久删除" }));
    await waitFor(() => expect(api.deleteSkill).toHaveBeenCalledWith("demo-folder"));
  });
});

describe("McpSettingsSection", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.clearAllMocks();
    api.getMcpSettings.mockResolvedValue(structuredClone(mcpSettings));
    api.saveMcpSettings.mockResolvedValue(structuredClone(mcpSettings));
    api.setMcpEnabled.mockResolvedValue({ ...structuredClone(mcpSettings), enabled: true });
    api.testMcpServer.mockResolvedValue({ protocol_version: "test", counts: { tools: 1, resources: 0, resource_templates: 0, prompts: 0 } });
  });

  it("loads protected JSON and tests a saved server", async () => {
    renderWithApp(<McpSettingsSection />);
    await screen.findByText("trace");
    expect(screen.getByLabelText("MCP JSON 配置")).toHaveValue(JSON.stringify({ mcpServers: mcpSettings.mcpServers }, null, 2));
    await userEvent.click(screen.getByRole("button", { name: "测试连接 trace" }));
    await waitFor(() => expect(api.testMcpServer).toHaveBeenCalledWith("trace"));
    await screen.findByText(/连接成功/);
  });

  it("saves the whole document and does not test stale settings", async () => {
    renderWithApp(<McpSettingsSection />);
    await screen.findByText("trace");
    const document = { mcpServers: { remote: { type: "sse", url: "http://127.0.0.1:1234/sse", headers: { Authorization: "test-token" } } } };
    fireEvent.change(screen.getByLabelText("MCP JSON 配置"), { target: { value: JSON.stringify(document) } });
    expect(screen.getByRole("button", { name: "测试连接 trace" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: /保存配置/ }));
    await waitFor(() => expect(api.saveMcpSettings).toHaveBeenCalledWith(document));
    await waitFor(() => expect(screen.getByLabelText("MCP JSON 配置")).not.toHaveValue(JSON.stringify(document)));
  });

  it("keeps a draft when toggling the global switch and can discard it", async () => {
    renderWithApp(<McpSettingsSection />);
    await screen.findByText("trace");
    fireEvent.change(screen.getByLabelText("MCP JSON 配置"), { target: { value: "draft" } });
    await userEvent.click(screen.getByRole("switch", { name: "启用 MCP" }));
    await waitFor(() => expect(api.setMcpEnabled).toHaveBeenCalledWith(true));
    expect(screen.getByLabelText("MCP JSON 配置")).toHaveValue("draft");
    await userEvent.click(screen.getByRole("button", { name: /撤销修改/ }));
    expect(screen.getByLabelText("MCP JSON 配置")).toHaveValue(JSON.stringify({ mcpServers: mcpSettings.mcpServers }, null, 2));
  });

  it("reports invalid JSON without echoing its contents", async () => {
    renderWithApp(<McpSettingsSection />);
    await screen.findByText("trace");
    fireEvent.change(screen.getByLabelText("MCP JSON 配置"), { target: { value: "invalid-secret-json" } });
    await userEvent.click(screen.getByRole("button", { name: /保存配置/ }));
    await screen.findByText("JSON 格式错误，请检查引号、逗号和括号。");
    expect(api.saveMcpSettings).not.toHaveBeenCalled();
  });

  it("displays the original connection failure", async () => {
    api.testMcpServer.mockRejectedValue(new Error("Connection refused"));
    renderWithApp(<McpSettingsSection />);
    await screen.findByText("trace");
    await userEvent.click(screen.getByRole("button", { name: "测试连接 trace" }));
    await screen.findByText("Connection refused");
  });
});
