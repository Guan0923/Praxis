import { BranchesOutlined, CommentOutlined, NodeIndexOutlined } from "@ant-design/icons";
import { Button, Dropdown, Tooltip } from "antd";
import AgentThreadPicker from "./AgentThreadPicker";
import { useState } from "react";
import ScrollingText from "../../components/ScrollingText";
import { useButtonTooltips } from "../../components/ButtonTooltipContext";

export type ChatMainView = "chat" | "trace";

interface AgentThreadPickerState {
  sessionId: string;
  rootThreadId: string;
  selectedThreadId: string;
  invalidation: number;
  onSelect: (threadId: string) => void;
}

interface ChatToolbarProps {
  visible: boolean;
  currentThreadId: string;
  conversationTitle: string;
  compact: boolean;
  mainView: ChatMainView;
  agentThread?: AgentThreadPickerState;
  onMainViewChange: (view: ChatMainView) => void;
}

export function ChatToolbar({
  visible,
  currentThreadId,
  conversationTitle,
  compact,
  mainView,
  agentThread,
  onMainViewChange,
}: ChatToolbarProps) {
  const showButtonTooltips = useButtonTooltips();
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <header className="trace-toolbar">
      <span className="chat-toolbar-title" title={conversationTitle}>{conversationTitle}</span>
      {visible ? <nav className="chat-toolbar-actions" aria-label="主内容视图">
      {agentThread ? (
        <AgentThreadPicker
          sessionId={agentThread.sessionId}
          rootThreadId={agentThread.rootThreadId}
          selectedThreadId={agentThread.selectedThreadId}
          compact={compact}
          invalidation={agentThread.invalidation}
          onSelect={agentThread.onSelect}
        />
      ) : (
        <Dropdown
          trigger={["click"]}
          onOpenChange={setMenuOpen}
          menu={{
            selectable: true,
            selectedKeys: [currentThreadId],
            items: [{ key: currentThreadId, label: currentThreadId }],
          }}
        >
          <Tooltip title={showButtonTooltips && compact ? "Thread" : undefined} open={menuOpen || !showButtonTooltips ? false : undefined}>
            <Button type="text" aria-label="Thread" aria-description={currentThreadId} icon={compact ? <BranchesOutlined /> : undefined}>
              {compact ? null : "Thread"}
            </Button>
          </Tooltip>
        </Dropdown>
      )}
      <ScrollingText text={currentThreadId} className="trace-toolbar-thread-id" focusable />
      <Tooltip title={showButtonTooltips && compact ? "Chat" : undefined}>
        <Button
          type="text"
          aria-label="Chat"
          icon={compact ? <CommentOutlined /> : undefined}
          aria-pressed={mainView === "chat"}
          onClick={() => onMainViewChange("chat")}
        >
          {compact ? null : "Chat"}
        </Button>
      </Tooltip>
      <Tooltip title={showButtonTooltips && compact ? "Trace" : undefined}>
        <Button
          type="text"
          aria-label="Trace"
          icon={compact ? <NodeIndexOutlined /> : undefined}
          aria-pressed={mainView === "trace"}
          onClick={() => onMainViewChange("trace")}
        >
          {compact ? null : "Trace"}
        </Button>
      </Tooltip>
      </nav> : null}
    </header>
  );
}
