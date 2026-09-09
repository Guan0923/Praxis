import { BranchesOutlined } from "@ant-design/icons";
import { Button, Popover, Tree, Tooltip } from "antd";
import { useEffect, useState, type Key, type ReactNode } from "react";
import { listAgentThreadChildren } from "../../api";
import type { AgentThreadSummary } from "../../types";
import ScrollingText from "../../components/ScrollingText";
import { useButtonTooltips } from "../../components/ButtonTooltipContext";

interface PickerNode {
  key: string;
  title: ReactNode;
  isLeaf?: boolean;
  children?: PickerNode[];
}

interface AgentThreadPickerProps {
  sessionId: string;
  rootThreadId: string;
  selectedThreadId: string;
  compact: boolean;
  invalidation: number;
  onSelect: (threadId: string) => void;
}

function nodeTitle(summary: AgentThreadSummary): ReactNode {
  const label = `${summary.thread_path} · ${summary.thread_status}`;
  return <Tooltip title={summary.task_result || undefined}><span className="thread-node-label"><ScrollingText text={label} /></span></Tooltip>;
}

function updateNode(nodes: PickerNode[], key: Key, children: PickerNode[]): PickerNode[] {
  return nodes.map((node) => {
    if (node.key === key) return { ...node, children, isLeaf: children.length === 0 };
    return node.children ? { ...node, children: updateNode(node.children, key, children) } : node;
  });
}

export default function AgentThreadPicker({
  sessionId,
  rootThreadId,
  selectedThreadId,
  compact,
  invalidation,
  onSelect,
}: AgentThreadPickerProps) {
  const showButtonTooltips = useButtonTooltips();
  const [open, setOpen] = useState(false);
  const [treeData, setTreeData] = useState<PickerNode[]>([
    { key: rootThreadId, title: "root", isLeaf: false },
  ]);

  useEffect(() => {
    setTreeData([{ key: rootThreadId, title: "root", isLeaf: false }]);
  }, [sessionId, rootThreadId, invalidation]);

  async function loadChildren(node: PickerNode): Promise<void> {
    if (node.children) return;
    const children = await listAgentThreadChildren(sessionId, String(node.key));
    setTreeData((current) => updateNode(
      current,
      node.key,
      children.map((item) => ({ key: item.thread_id, title: nodeTitle(item), isLeaf: false })),
    ));
  }

  const picker = (
    <Tree<PickerNode>
      key={`${rootThreadId}:${invalidation}`}
      aria-label="Agent Thread 树"
      className="agent-thread-tree"
      blockNode
      loadData={loadChildren}
      selectedKeys={[selectedThreadId]}
      treeData={treeData}
      onSelect={(keys) => {
        const selected = keys[0];
        if (selected == null) return;
        onSelect(String(selected));
        setOpen(false);
      }}
    />
  );

  return (
    <Popover
      classNames={{ root: "agent-thread-popover" }}
      styles={{ container: { padding: 8, borderRadius: 8, border: "1px solid var(--border)", background: "var(--surface)", boxShadow: "0 4px 12px var(--shadow-soft)" } }}
      content={picker}
      open={open}
      placement="bottom"
      trigger="click"
      onOpenChange={setOpen}
    >
      <Tooltip title={showButtonTooltips && compact ? "Thread" : undefined} open={open || !showButtonTooltips ? false : undefined}>
        <Button type="text" aria-label="Thread" aria-description={selectedThreadId} icon={compact ? <BranchesOutlined /> : undefined}>
          {compact ? null : "Thread"}
        </Button>
      </Tooltip>
    </Popover>
  );
}
