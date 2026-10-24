import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import AppSidebar from "./AppSidebar";
import type { Conversation } from "../types";

const conversation: Conversation = {
  id: "c1",
  title: "测试对话",
  messages: [],
  messageCount: 3,
  updatedAt: "2026-08-05T10:20:00Z",
};

const projectConversation: Conversation = {
  id: "project-c1",
  title: "项目对话",
  messages: [],
  messageCount: 1,
  projectId: "project-1",
};

const project = {
  project_id: "project-1",
  name: "示例项目",
  cwd: "C:\\workspace\\example",
  available: true,
  created_at: "2026-08-05T10:20:00Z",
  updated_at: "2026-08-05T10:20:00Z",
  conversation_count: 1,
  session_ids: ["project-c1"],
};

function renderSidebar(archivedCount = 0, conversations: Conversation[] = [conversation]) {
  const onNavigate = vi.fn();
  const onProfileUpdate = vi.fn().mockResolvedValue(undefined);
  const view = render(
    <AppSidebar
      profile={{ display_name: "账户名称", agent_preferences: "" }}
      conversations={conversations}
      archivedCount={archivedCount}
      currentId={conversation.id}
      page="chat"
      onNew={vi.fn()}
      onSelect={vi.fn()}
      onNavigate={onNavigate}
      onRename={vi.fn().mockResolvedValue(undefined)}
      onArchive={vi.fn().mockResolvedValue(undefined)}
      onDelete={vi.fn()}
      onProfileUpdate={onProfileUpdate}
    />,
  );
  return { onNavigate, onProfileUpdate, view };
}

describe("AppSidebar utility navigation", () => {
  it("separates icon navigation from the workspace explorer and keeps rail actions functional", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    const onOpenSettings = vi.fn();
    render(
      <AppSidebar
        profile={{ display_name: "本地用户", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={3}
        currentId={conversation.id}
        page="benchmark"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={onNavigate}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
        onOpenSettings={onOpenSettings}
      />,
    );

    const rail = screen.getByRole("navigation", { name: "工作台导航" });
    const explorer = screen.getByRole("region", { name: "工作空间" });
    expect(rail.parentElement).toBe(explorer.parentElement);
    expect(within(rail).getByRole("img", { name: "Praxis" })).toBeInTheDocument();
    expect(within(explorer).getByRole("button", { name: "新建对话" })).toBeInTheDocument();
    expect(within(explorer).getByRole("button", { name: "新建项目" })).toBeInTheDocument();
    expect(within(rail).queryByRole("button", { name: "新建对话" })).not.toBeInTheDocument();
    expect(within(explorer).queryByRole("button", { name: "Benchmark" })).not.toBeInTheDocument();
    for (const name of ["对话工作台", "Benchmark", "回收站 (3)"]) {
      const action = within(rail).getByRole("button", { name });
      expect(action).toHaveClass("sidebar-rail-action");
      expect(action.textContent?.trim()).toBe("");
      expect(action).toHaveAttribute("title");
    }
    const chat = within(rail).getByRole("button", { name: "对话工作台" });
    expect(chat).not.toHaveAttribute("aria-current");
    expect(within(rail).getByRole("button", { name: "Benchmark" })).toHaveAttribute("aria-current", "page");
    chat.focus();
    await user.keyboard("{Enter}");
    expect(onNavigate).toHaveBeenCalledWith("chat");
    await user.click(within(rail).getByRole("button", { name: "个人简介：本地用户" }));
    expect(onOpenSettings).toHaveBeenCalledOnce();
  });

  it("keeps sidebar content and its animation class across page navigation", () => {
    const props = {
      profile: { display_name: "Local", agent_preferences: "" },
      conversations: [conversation], archivedCount: 0, currentId: conversation.id,
      onNew: vi.fn(), onSelect: vi.fn(), onNavigate: vi.fn(),
      onRename: vi.fn(), onArchive: vi.fn(), onDelete: vi.fn(),
    };
    const { rerender, container } = render(<AppSidebar {...props} page="chat" />);
    const shell = container.querySelector(".sidebar-reveal-shell");
    const children = Array.from(shell!.querySelectorAll(".sidebar-primary-actions button, .sidebar-reveal-item, .project-history-list"));
    expect(shell).toHaveClass("sidebar-reveal-active");
    const animationEnd = new Event("animationend", { bubbles: true });
    Object.defineProperty(animationEnd, "animationName", { value: "chat-sidebar-reveal" });
    fireEvent(shell!.querySelector('[data-reveal-index="5"]')!, animationEnd);
    for (const page of ["benchmark", "chat", "trash", "chat"] as const) {
      rerender(<AppSidebar {...props} page={page} />);
      expect(container.querySelector(".sidebar-reveal-shell")).toBe(shell);
      expect(shell).not.toHaveClass("sidebar-reveal-active");
      expect(children.filter((child) => !shell!.contains(child)).map((child) => child.outerHTML)).toEqual([]);
    }
  });

  it("renders the project title and collapse control in the sidebar header", async () => {
    const user = userEvent.setup();
    const onToggleCollapse = vi.fn();
    render(
      <AppSidebar
        profile={{ display_name: "账户名称", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        collapsed={false}
        onToggleCollapse={onToggleCollapse}
      />,
    );

    const header = document.querySelector(".sidebar-header");
    expect(header).not.toBeNull();
    expect(within(header as HTMLElement).getByText("Praxis")).toBeInTheDocument();
    expect(within(header as HTMLElement).getByText("工作空间")).toHaveClass("sidebar-project-title");
    expect(screen.getByRole("button", { name: "折叠侧边栏" })).toHaveAttribute("aria-expanded", "true");
    expect(document.querySelectorAll(".sidebar-reveal-item")).toHaveLength(6);
    await user.click(screen.getByRole("button", { name: "折叠侧边栏" }));
    expect(onToggleCollapse).toHaveBeenCalledOnce();
  });

  it("keeps the rail available while hiding only the explorer when collapsed", () => {
    render(
      <AppSidebar
        profile={{ display_name: "账户名称", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        collapsed
        onToggleCollapse={vi.fn()}
      />,
    );

    const rail = screen.getByRole("navigation", { name: "工作台导航" });
    const chat = within(rail).getByRole("button", { name: "对话工作台" });
    expect(chat).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById(chat.getAttribute("aria-controls")!)).toHaveAttribute("hidden");
    expect(screen.queryByRole("region", { name: "工作空间" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "展开侧边栏" })).not.toBeInTheDocument();
  });

  it("reuses the rail chat button to toggle the explorer and return from other pages", () => {
    const onToggleCollapse = vi.fn();
    const onNavigate = vi.fn();
    const props = {
      profile: { display_name: "Local", agent_preferences: "" },
      conversations: [conversation], archivedCount: 0, currentId: conversation.id,
      onNew: vi.fn(), onSelect: vi.fn(), onNavigate,
      onRename: vi.fn(), onArchive: vi.fn(), onDelete: vi.fn(), onToggleCollapse,
    };
    const { rerender } = render(<AppSidebar {...props} page="chat" />);
    const rail = screen.getByRole("navigation", { name: "工作台导航" });
    const chat = within(rail).getByRole("button", { name: "对话工作台" });
    fireEvent.click(chat);
    expect(onToggleCollapse).toHaveBeenCalledTimes(1);
    expect(onNavigate).not.toHaveBeenCalled();
    rerender(<AppSidebar {...props} page="chat" collapsed />);
    expect(screen.getByRole("navigation", { name: "工作台导航" })).toBe(rail);
    fireEvent.click(chat);
    expect(onToggleCollapse).toHaveBeenCalledTimes(2);
    rerender(<AppSidebar {...props} page="benchmark" />);
    fireEvent.click(chat);
    expect(onNavigate).toHaveBeenLastCalledWith("chat");
    expect(onToggleCollapse).toHaveBeenCalledTimes(2);
    rerender(<AppSidebar {...props} page="trash" collapsed />);
    fireEvent.click(chat);
    expect(onNavigate).toHaveBeenLastCalledWith("chat");
    expect(onToggleCollapse).toHaveBeenCalledTimes(3);
  });

  it("uses the shared create-button style for new conversations and projects", async () => {
    const user = userEvent.setup();
    const onNew = vi.fn();
    const onNewProject = vi.fn();

    render(
      <AppSidebar
        profile={{ display_name: "账户名称", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={onNew}
        onNewProject={onNewProject}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
      />,
    );

    const newConversationButton = screen.getByRole("button", { name: "新建对话" });
    const newProjectButton = screen.getByRole("button", { name: "新建项目" });
    expect(newConversationButton).toHaveClass("sidebar-create-button");
    expect(newProjectButton).toHaveClass("sidebar-create-button");
    expect(newConversationButton).not.toHaveClass("ant-btn-lg");
    expect(newProjectButton).not.toHaveClass("ant-btn-lg");

    await user.click(newConversationButton);
    await user.click(newProjectButton);
    expect(onNew).toHaveBeenCalledOnce();
    expect(onNewProject).toHaveBeenCalledOnce();
  });

  it("shows the unread archive count and navigates from both utility buttons", async () => {
    const user = userEvent.setup();
    const { onNavigate, view } = renderSidebar(2);

    expect(screen.getByRole("button", { name: "回收站 (2)" })).toBeInTheDocument();
    expect(view.container.querySelector(".sidebar-utility-links")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "回收站 (2)" }));
    await user.click(screen.getByRole("button", { name: "Benchmark" }));
    expect(onNavigate).toHaveBeenNthCalledWith(1, "trash");
    expect(onNavigate).toHaveBeenNthCalledWith(2, "benchmark");
  });

  it("marks the active utility destination without changing navigation", async () => {
    const { view, onNavigate } = renderSidebar();
    const props = {
      profile: { display_name: "本地用户", agent_preferences: "" },
      conversations: [conversation], archivedCount: 0, currentId: conversation.id,
      onNew: vi.fn(), onSelect: vi.fn(), onNavigate,
      onRename: vi.fn(), onArchive: vi.fn(), onDelete: vi.fn(),
    };

    for (const page of ["trash", "benchmark", "chat"] as const) {
      view.rerender(<AppSidebar {...props} page={page} />);
      for (const [destination, name] of [["chat", "对话工作台"], ["trash", "回收站"], ["benchmark", "Benchmark"]] as const) {
        const button = screen.getByRole("button", { name });
        if (page === destination) {
          expect(button).toHaveAttribute("aria-current", "page");
          expect(button).toHaveClass("sidebar-nav-active");
        } else {
          expect(button).not.toHaveAttribute("aria-current");
          expect(button).not.toHaveClass("sidebar-nav-active");
        }
      }
    }
    await userEvent.click(screen.getByRole("button", { name: "Benchmark" }));
    expect(onNavigate).toHaveBeenCalledWith("benchmark");
  });

  it("renders project history above ordinary history and keeps the selected project collapsible", async () => {
    const user = userEvent.setup();
    render(
      <AppSidebar
        profile={{ display_name: "账户名称", agent_preferences: "" }}
        conversations={[conversation, projectConversation]}
        projects={[project]}
        archivedCount={0}
        currentId={projectConversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
      />,
    );

    const projectHeading = screen.getByText("项目对话", { selector: ".ant-typography" });
    const ordinaryHeading = screen.getByText("无项目对话");
    expect(projectHeading.compareDocumentPosition(ordinaryHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const item = document.querySelector(".ant-collapse-item");
    expect(item).not.toBeNull();
    await waitFor(() => expect(item).toHaveClass("ant-collapse-item-active"));
    await user.click(item?.querySelector(".ant-collapse-header") as HTMLElement);
    await waitFor(() => expect(item).not.toHaveClass("ant-collapse-item-active"));

    const projectToggle = screen.getByRole("button", { name: "项目对话" });
    const ordinaryToggle = screen.getByRole("button", { name: "无项目对话" });
    await user.click(projectToggle);
    expect(projectToggle).toHaveAttribute("aria-expanded", "false");
    expect(document.querySelector(".project-history-list")).not.toBeVisible();
    expect(ordinaryToggle).toHaveAttribute("aria-expanded", "true");
    await user.click(ordinaryToggle);
    expect(ordinaryToggle).toHaveAttribute("aria-expanded", "false");
    await user.click(projectToggle);
    await user.click(ordinaryToggle);
    expect(document.querySelector(".project-history-list")).toBeVisible();
    expect(ordinaryToggle).toHaveAttribute("aria-expanded", "true");
  });

  it("sorts project and ordinary groups independently", async () => {
    const user = userEvent.setup();
    const onSort = vi.fn().mockResolvedValue(undefined);
    const projectSecond: Conversation = { ...projectConversation, id: "project-c2", title: "项目对话 2" };
    const ordinarySecond: Conversation = { ...conversation, id: "c2", title: "普通对话 2" };
    render(
      <AppSidebar
        profile={{ display_name: "账户名称", agent_preferences: "" }}
        conversations={[projectConversation, projectSecond, conversation, ordinarySecond]}
        projects={[{ ...project, conversation_count: 2, session_ids: ["project-c1", "project-c2"] }]}
        archivedCount={0}
        currentId={projectConversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        onReorder={vi.fn().mockResolvedValue(undefined)}
        onSort={onSort}
      />,
    );

    const sortButtons = screen.getAllByRole("button", { name: "对话排序" });
    await user.click(sortButtons[0]);
    await user.click(await screen.findByText("按最近聊天"));
    await waitFor(() => expect(onSort).toHaveBeenCalledWith("project-1", "recent_activity"));

    await user.click(sortButtons[1]);
    const createdAtOptions = await screen.findAllByText("按创建时间");
    const visibleCreatedAtOption = createdAtOptions.find(
      (option) => !option.closest(".ant-dropdown")?.classList.contains("ant-dropdown-hidden"),
    );
    if (!visibleCreatedAtOption) throw new Error("visible ordinary-group sort option was not rendered");
    await user.click(visibleCreatedAtOption);
    await waitFor(() => expect(onSort).toHaveBeenCalledWith(null, "created_at"));
  });

  it("keeps row selection and action buttons separate from drag activation", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <AppSidebar
        profile={{ display_name: "账户名称", agent_preferences: "" }}
        conversations={[conversation, { ...conversation, id: "c2", title: "第二个对话" }]}
        archivedCount={0}
        currentId={null}
        page="chat"
        onNew={vi.fn()}
        onSelect={onSelect}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        onReorder={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(screen.getByRole("listitem", { name: "拖动排序：测试对话" })).toHaveAttribute("aria-roledescription", "可排序对话");
    await user.click(screen.getByRole("button", { name: "测试对话" }));
    expect(onSelect).toHaveBeenCalledWith("c1");
    await user.click(screen.getByRole("button", { name: "更多操作：测试对话" }));
    expect(screen.getByText("重命名")).toBeInTheDocument();
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("shows project settings as list buttons and dispatches rename, path, and removal actions", async () => {
    const user = userEvent.setup();
    const onRenameProject = vi.fn().mockResolvedValue(undefined);
    const onChangeProjectPath = vi.fn().mockResolvedValue(undefined);
    const onRemoveProject = vi.fn().mockResolvedValue(undefined);
    render(
      <AppSidebar
        profile={{ display_name: "账户名称", agent_preferences: "" }}
        conversations={[projectConversation]}
        projects={[project]}
        archivedCount={0}
        currentId={null}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        onRenameProject={onRenameProject}
        onChangeProjectPath={onChangeProjectPath}
        onRemoveProject={onRemoveProject}
      />,
    );

    await user.click(screen.getByRole("button", { name: "项目设置 示例项目" }));
    expect(screen.getByRole("button", { name: "修改项目名称" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "修改项目路径" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除项目" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "修改项目名称" }));
    const nameInput = screen.getByRole("textbox", { name: "项目名称" });
    await user.clear(nameInput);
    await user.type(nameInput, "重命名项目");
    await user.click(screen.getByRole("button", { name: /保.*存/ }));
    await waitFor(() => expect(onRenameProject).toHaveBeenCalledWith("project-1", "重命名项目"));

    await user.click(screen.getByRole("button", { name: "项目设置 示例项目" }));
    await user.click(screen.getByRole("button", { name: "修改项目路径" }));
    await waitFor(() => expect(onChangeProjectPath).toHaveBeenCalledWith("project-1"));

    await user.click(screen.getByRole("button", { name: "项目设置 示例项目" }));
    await user.click(screen.getByRole("button", { name: "删除项目" }));
    await user.click(screen.getByRole("button", { name: /移.*除/ }));
    await waitFor(() => expect(onRemoveProject).toHaveBeenCalledWith("project-1"));
  });

  it("does not render an archive badge when there are no unread archives", () => {
    renderSidebar();
    expect(screen.getByRole("button", { name: "回收站" })).toBeInTheDocument();
    expect(document.querySelector(".ant-badge-count")).not.toBeInTheDocument();
  });
  it("uses only the spinner for a running conversation", () => {
    const running: Conversation = {
      ...conversation,
      messages: [{ id: "assistant-1", role: "assistant", content: "", events: [], running: true }],
    };
    renderSidebar(0, [running]);

    expect(document.querySelector(".ant-spin")).toBeInTheDocument();
    expect(document.querySelector(".ant-badge-status")).not.toBeInTheDocument();
  });
  it("opens the profile card and saves the username and agent preferences", async () => {
    const user = userEvent.setup();
    const { onProfileUpdate } = renderSidebar();

    await user.click(screen.getByRole("button", { name: "个人简介：账户名称" }));
    expect(screen.getByText("个人简介", { selector: ".ant-popover-title" })).toBeInTheDocument();
    await user.clear(screen.getByRole("textbox", { name: "用户名" }));
    await user.type(screen.getByRole("textbox", { name: "用户名" }), "小明");
    await user.type(screen.getByRole("textbox", { name: "Agent 偏好" }), "先给结论，再给步骤");
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(onProfileUpdate).toHaveBeenCalledWith({
      display_name: "小明",
      agent_preferences: "先给结论，再给步骤",
    });
  });
  it("uses the guest username instead of an email fallback", () => {
    render(
      <AppSidebar
        profile={{ display_name: "本地用户", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        onProfileUpdate={vi.fn().mockResolvedValue(undefined)}
      />,
    );
    expect(screen.getByRole("button", { name: "个人简介：本地用户" })).toBeInTheDocument();
  });

  it("shows the guest username when settings are opened from the chat sidebar", () => {
    render(
      <AppSidebar
        profile={{ display_name: "本地用户", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        onOpenSettings={vi.fn()}
      />,
    );
    const trigger = screen.getByRole("button", { name: "个人简介：本地用户" });
    expect(trigger).toHaveTextContent("本地用户");
    expect(trigger.querySelector(".profile-trigger-label-text")).toHaveTextContent("本地用户");
  });

  it("truncates long names and scrolls them only while hovered", () => {
    render(
      <AppSidebar
        profile={{ display_name: "这是一个很长的用户名称用于测试悬浮循环滚动", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        onOpenSettings={vi.fn()}
      />,
    );
    const viewport = document.querySelector<HTMLElement>(".profile-trigger-label-viewport");
    const text = document.querySelector<HTMLElement>(".profile-trigger-label-text");
    if (!viewport || !text) throw new Error("profile label was not rendered");
    Object.defineProperty(viewport, "clientWidth", { configurable: true, value: 80 });
    Object.defineProperty(text, "scrollWidth", { configurable: true, value: 240 });

    fireEvent.mouseEnter(viewport);
    expect(viewport).toHaveClass("is-scrolling");
    fireEvent.mouseLeave(viewport);
    expect(viewport).not.toHaveClass("is-scrolling");
  });

  it("does not start marquee scrolling for a short name", () => {
    render(
      <AppSidebar
        profile={{ display_name: "小明", agent_preferences: "" }}
        conversations={[conversation]}
        archivedCount={0}
        currentId={conversation.id}
        page="chat"
        onNew={vi.fn()}
        onSelect={vi.fn()}
        onNavigate={vi.fn()}
        onRename={vi.fn().mockResolvedValue(undefined)}
        onArchive={vi.fn().mockResolvedValue(undefined)}
        onDelete={vi.fn()}
        onOpenSettings={vi.fn()}
      />,
    );
    const viewport = document.querySelector<HTMLElement>(".profile-trigger-label-viewport");
    const text = document.querySelector<HTMLElement>(".profile-trigger-label-text");
    if (!viewport || !text) throw new Error("profile label was not rendered");
    Object.defineProperty(viewport, "clientWidth", { configurable: true, value: 160 });
    Object.defineProperty(text, "scrollWidth", { configurable: true, value: 32 });

    fireEvent.mouseEnter(viewport);
    expect(viewport).not.toHaveClass("is-scrolling");
  });
  it("renders history metadata and scrolls only the hovered overflowing item", () => {
    const second: Conversation = { id: "c2", title: "第二个会话", messages: [] };
    const longTitle = "这是一个足够长的历史会话摘要，用于验证悬浮时只滚动当前条目";
    renderSidebar(0, [{
      ...conversation,
      title: longTitle,
      messageCount: 12,
      messages: [{ id: "loaded-message", role: "user", content: "已打开", events: [] }],
    }, second]);

    expect(screen.getByText(/12 条消息/)).toBeInTheDocument();
    expect(document.querySelector(".history-meta")?.textContent).toMatch(/12 条消息 ·/);

    const viewports = Array.from(document.querySelectorAll<HTMLElement>(".history-summary-viewport"));
    const firstText = viewports[0].querySelector<HTMLElement>(".history-summary-text");
    const secondText = viewports[1].querySelector<HTMLElement>(".history-summary-text");
    expect(firstText).not.toBeNull();
    expect(secondText).not.toBeNull();
    Object.defineProperty(viewports[0], "clientWidth", { configurable: true, value: 80 });
    Object.defineProperty(firstText, "scrollWidth", { configurable: true, value: 240 });
    Object.defineProperty(viewports[1], "clientWidth", { configurable: true, value: 160 });
    Object.defineProperty(secondText, "scrollWidth", { configurable: true, value: 160 });

    fireEvent.mouseEnter(viewports[0]);
    expect(viewports[0]).toHaveClass("is-scrolling");
    expect(viewports[1]).not.toHaveClass("is-scrolling");
    fireEvent.mouseLeave(viewports[0]);
    expect(viewports[0]).not.toHaveClass("is-scrolling");
  });
});
