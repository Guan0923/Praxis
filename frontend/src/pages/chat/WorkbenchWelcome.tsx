import { CodeOutlined, CompassOutlined, FileSearchOutlined } from "@ant-design/icons";
import Prompts from "@ant-design/x/es/prompts";
import Welcome from "@ant-design/x/es/welcome";

export const WORKBENCH_STARTERS = [
  { key: "explore", icon: <CompassOutlined />, label: "了解项目", description: "梳理结构，找到关键入口", prompt: "请先阅读当前项目，梳理目录结构、核心模块和运行方式，先不要修改文件。" },
  { key: "implement", icon: <CodeOutlined />, label: "实现想法", description: "从清晰的方案开始", prompt: "我想在当前项目实现一个新功能。请先了解现有实现，再和我确认需求与实施方案。" },
  { key: "investigate", icon: <FileSearchOutlined />, label: "排查问题", description: "定位原因，验证修复", prompt: "请帮我排查当前项目的问题。先了解相关代码和测试，等我提供具体的复现步骤后再修改。" },
];

interface WorkbenchWelcomeProps {
  disabled?: boolean;
  onSelectPrompt?: (prompt: string) => void;
  heroOnly?: boolean;
}

export default function WorkbenchWelcome({ disabled, onSelectPrompt, heroOnly }: WorkbenchWelcomeProps) {
  return (
    <section className="welcome workbench-welcome" aria-label="Praxis 工作台">
      <div className="workbench-eyebrow"><span className="workbench-status-dot" /> PRAXIS / YOUR LOCAL AGENT</div>
      <Welcome
        variant="borderless"
        className="workbench-welcome-heading"
        title={<>把想法交给 Praxis<span className="workbench-title-dot">.</span></>}
        description="从一个问题开始，到真正完成。"
      />
      {!heroOnly ? <WorkbenchStarters disabled={disabled} onSelectPrompt={onSelectPrompt} /> : null}
    </section>
  );
}

export function WorkbenchStarters({ disabled, onSelectPrompt }: WorkbenchWelcomeProps) {
  return <div className="workbench-starters">
      <Prompts
        className="workbench-prompts"
        wrap
        items={WORKBENCH_STARTERS.map((item) => ({
          key: item.key,
          disabled: disabled || !onSelectPrompt,
          label: <button type="button" className="workbench-prompt-button" disabled={disabled || !onSelectPrompt}>
            <span className="workbench-prompt-icon" aria-hidden="true">{item.icon}</span>
            <span className="workbench-prompt-label">{item.label}</span>
            <span className="workbench-prompt-description">{item.description}</span>
          </button>,
        }))}
        onItemClick={({ data }) => {
          const starter = WORKBENCH_STARTERS.find((item) => item.key === data.key);
          if (starter && !disabled) onSelectPrompt?.(starter.prompt);
        }}
      />
      <p className="workbench-welcome-note">建议只会填入输入框，由你确认后发送。</p>
    </div>;
}
