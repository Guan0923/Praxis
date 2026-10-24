# 侧边栏领域组件

该包拆分会话历史、项目设置和个人资料交互，由 `../AppSidebar.tsx` 组合。

- `ConversationHistory.tsx`：`HistoryRow`、重命名/归档/删除菜单和公开的 `confirmDelete`。
- `ProjectSettings.tsx`：项目改名、路径选择、Skill 信任撤销和移除入口。
- `ProfileSection.tsx`：`ProfilePopover`、`ProfileLabel` 与个人资料校验。
- `useHorizontalOverflow.ts`：共享 ResizeObserver 溢出测量。
- `useProjectExpansion.ts`：项目 Collapse 展开状态及 localStorage 持久化。
- `types.ts`：`AppSidebarProps` 与历史变更回调契约。

该包只发出回调，不直接调用项目/会话 API；侧栏展示样式集中在 `../../styles/overrides-sidebar.css`，使用现有中性色和 `--accent` 色板。

## 工作台布局

- 桌面展开为 **54px 导航轨 + 弹性工作空间浏览器**，总宽 280px；折叠时 Sider 保留 54px，只隐藏工作空间 `section`，不卸载项目与历史。移动端仍使用完整 Drawer。
- 对话工作台图标同时控制工作空间展开 / 折叠；从其他页面点击先返回对话，若已折叠则展开。工作空间标题处按钮仅用于折叠；不提供悬浮展开按钮。`aria-controls` 指向当前实例的工作空间区域。
- 导航轨顶部为独立 Praxis 标记与返回对话工作台入口；底部为 Benchmark、带归档计数的回收站、个人简介 / 设置图标。图标保留可访问名称、悬停提示与键盘焦点，当前导航通过 `aria-current` 标识。共享资料编辑器仍可用，用户名在轨道内视觉隐藏，不再占用一整行。
- 浏览器包含工作空间标题、侧栏折叠按钮、并排的紧凑新建对话 / 项目操作，以及独立滚动的项目与无项目历史组。保留原有中文操作标签与组折叠状态。
- 项目树、排序、拖动、重命名、归档、删除、项目设置及目录不可用提示均沿用现有回调和审批语义；选中历史使用强调色细边与浅色底，项目会话保留树形缩进。
- 减少动态效果偏好下停用动画；无虚构搜索或未绑定行为的操作。

## 验证

在 `frontend` 执行：

```powershell
npm test -- --run src/components/AppSidebar.test.tsx src/components/sidebar/SortableHistoryList.test.ts src/app/sidebarRefresh.test.ts src/api/conversations/sidebarThreads.test.ts
antd lint .\src\components\AppSidebar.tsx --format json
npm run typecheck
```
