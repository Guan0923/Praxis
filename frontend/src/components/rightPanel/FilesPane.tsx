import {
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  FileAddOutlined,
  FileImageOutlined,
  FileOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  FolderOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MoreOutlined,
  ReloadOutlined,
  SwapOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Dropdown,
  Empty,
  Grid,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Splitter,
  Tree,
  Tooltip,
  Typography,
  type MenuProps,
} from "antd";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type Key, type ReactNode } from "react";
import {
  createFileEntry,
  getFileRoots,
  listFileDirectory,
  moveFileEntry,
  readEditorFile,
  recycleFileEntry,
  renameFileEntry,
  saveEditorFile,
  sessionFileContentUrl,
} from "../../api";
import { ApiError } from "../../api/transport/request";
import type { FileEditorDocument, FileTreeEntry, FileTreeRoot, ManagedFileSource, RightPanelWindow } from "../../types";
import { registerFilePanelCloseGuard } from "./filePanelLifecycle";

const CodeEditor = lazy(() => import("./CodeEditor"));
const SAVE_DELAY_MS = 800;
const REFRESH_DELAY_MS = 3_000;
const DEFAULT_ROOTS: FileTreeRoot[] = [
  { source: "workspace", path: "workspace:", name: "workspace", available: true },
  { source: "project", path: "project:", name: "project", available: false },
];

interface FileTreeNode {
  key: string;
  title: string;
  isLeaf?: boolean;
  disabled?: boolean;
  children?: FileTreeNode[];
  source?: ManagedFileSource;
  path?: string;
  kind?: FileTreeEntry["kind"] | "root";
  entry?: FileTreeEntry;
  icon?: ReactNode;
}

function updateTree(nodes: FileTreeNode[], key: Key, updater: (node: FileTreeNode) => FileTreeNode): FileTreeNode[] {
  return nodes.map((node) => {
    if (node.key === key) return updater(node);
    return node.children ? { ...node, children: updateTree(node.children, key, updater) } : node;
  });
}

function findNode(nodes: FileTreeNode[], key: Key): FileTreeNode | undefined {
  for (const node of nodes) {
    if (node.key === key) return node;
    const child = node.children ? findNode(node.children, key) : undefined;
    if (child) return child;
  }
  return undefined;
}

function entryNode(entry: FileTreeEntry): FileTreeNode {
  return {
    key: `${entry.source}:${entry.path}`,
    title: entry.name,
    isLeaf: entry.kind !== "directory",
    disabled: entry.kind === "link" || entry.kind === "special",
    source: entry.source,
    path: entry.path,
    kind: entry.kind,
    entry,
    icon: entry.kind === "directory" ? <FolderOutlined /> : entry.is_image ? <FileImageOutlined /> : <FileOutlined />,
  };
}

function rootNodes(roots: FileTreeRoot[]): FileTreeNode[] {
  return [{
    key: "files-root",
    title: "文件",
    kind: "root",
    isLeaf: false,
    children: roots.map((root) => ({
      key: root.path,
      title: root.name,
      source: root.source,
      path: root.path,
      kind: "root",
      isLeaf: false,
      disabled: !root.available,
      icon: <FolderOutlined />,
    })),
    icon: <FolderOutlined />,
  }];
}

interface DirectoryPickerProps {
  sessionId: string;
  selected: string | null;
  onSelect: (source: ManagedFileSource, path: string) => void;
}

function DirectoryPicker({ sessionId, selected, onSelect }: DirectoryPickerProps) {
  const [nodes, setNodes] = useState<FileTreeNode[]>(() => rootNodes(DEFAULT_ROOTS));
  useEffect(() => {
    let active = true;
    void getFileRoots(sessionId).then((roots) => {
      if (active) setNodes(rootNodes(roots));
    });
    return () => { active = false; };
  }, [sessionId]);
  const load = async (node: FileTreeNode) => {
    if (!node.source || !node.path || node.children) return;
    const entries = await listFileDirectory(sessionId, node.source, node.path);
    setNodes((current) => updateTree(current, node.key, (item) => ({
      ...item,
      children: entries.filter((entry) => entry.kind === "directory").map(entryNode),
    })));
  };
  return (
    <Tree<FileTreeNode>
      aria-label="移动目标目录"
      blockNode
      defaultExpandedKeys={["files-root"]}
      loadData={load}
      selectedKeys={selected ? [selected] : []}
      showIcon
      treeData={nodes}
      onSelect={(_keys, info) => {
        const node = info.node;
        if (node.source && node.path && (node.kind === "root" || node.kind === "directory")) {
          onSelect(node.source, node.path);
        }
      }}
    />
  );
}

interface FilesPaneProps {
  panelWindow: RightPanelWindow;
  active: boolean;
}

export default function FilesPane({ panelWindow, active }: FilesPaneProps) {
  const { message, modal } = App.useApp();
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;
  const sessionId = panelWindow.session_id;
  const [treeNodes, setTreeNodes] = useState<FileTreeNode[]>(() => rootNodes(DEFAULT_ROOTS));
  const [loadedKeys, setLoadedKeys] = useState<Key[]>([]);
  const [expandedKeys, setExpandedKeys] = useState<Key[]>(["files-root"]);
  const [selected, setSelected] = useState<FileTreeEntry | null>(null);
  const [document, setDocument] = useState<FileEditorDocument | null>(null);
  const [draft, setDraft] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "failed" | "conflict">("idle");
  const [loadingFile, setLoadingFile] = useState(false);
  const [treeCollapsed, setTreeCollapsed] = useState(isMobile);
  const [treeWidth, setTreeWidth] = useState(260);
  const [action, setAction] = useState<{ type: "new-file" | "new-directory" | "rename"; node: FileTreeNode } | null>(null);
  const [actionValue, setActionValue] = useState("");
  const [moveNode, setMoveNode] = useState<FileTreeNode | null>(null);
  const [moveTarget, setMoveTarget] = useState<{ source: ManagedFileSource; path: string } | null>(null);
  const generationRef = useRef(0);
  const treeNodesRef = useRef(treeNodes);
  const loadedKeysRef = useRef(loadedKeys);
  const saveTimerRef = useRef<number | null>(null);
  const saveQueueRef = useRef(Promise.resolve(true));
  const selectedRef = useRef(selected);
  const documentRef = useRef(document);
  const draftRef = useRef(draft);
  const dirtyRef = useRef(dirty);
  const saveStateRef = useRef(saveState);
  selectedRef.current = selected;
  documentRef.current = document;
  draftRef.current = draft;
  dirtyRef.current = dirty;
  saveStateRef.current = saveState;
  treeNodesRef.current = treeNodes;
  loadedKeysRef.current = loadedKeys;

  const refreshRoots = useCallback(async () => {
    const roots = await getFileRoots(sessionId);
    setTreeNodes(rootNodes(roots));
    setLoadedKeys([]);
  }, [sessionId]);

  const refreshLoadedDirectories = useCallback(async () => {
    const roots = rootNodes(await getFileRoots(sessionId));
    const previous = treeNodesRef.current;
    const keys = loadedKeysRef.current.filter((key) => key !== "files-root");
    const listings = new Map<Key, FileTreeEntry[]>();
    await Promise.all(keys.map(async (key) => {
      const node = findNode(previous, key) ?? findNode(roots, key);
      if (!node?.source || !node.path || node.disabled) return;
      listings.set(key, await listFileDirectory(sessionId, node.source, node.path));
    }));
    const hydrate = (nodes: FileTreeNode[]): FileTreeNode[] => nodes.map((node) => {
      const entries = listings.get(node.key);
      const previousNode = findNode(previous, node.key);
      const children = entries?.map(entryNode) ?? previousNode?.children ?? node.children;
      return { ...node, children: children ? hydrate(children) : children };
    });
    setTreeNodes(hydrate(roots));
  }, [sessionId]);

  useEffect(() => {
    generationRef.current += 1;
    setSelected(null);
    setDocument(null);
    setDraft("");
    setDirty(false);
    setSaveState("idle");
    void refreshRoots().catch((error) => void message.error(String((error as Error).message ?? error)));
  }, [message, refreshRoots]);

  useEffect(() => {
    if (isMobile) setTreeCollapsed(true);
  }, [isMobile]);

  const loadNode = useCallback(async (node: FileTreeNode) => {
    if (!node.source || !node.path || node.disabled) return;
    const entries = await listFileDirectory(sessionId, node.source, node.path);
    setTreeNodes((current) => updateTree(current, node.key, (item) => ({
      ...item,
      children: entries.map(entryNode),
    })));
  }, [sessionId]);

  const saveNow = useCallback((force = false): Promise<boolean> => {
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    const entry = selectedRef.current;
    const current = documentRef.current;
    const content = draftRef.current;
    if (!dirtyRef.current || !entry || current?.kind !== "text") return Promise.resolve(true);
    const generation = generationRef.current;
    const run = async () => {
      setSaveState("saving");
      try {
        const saved = await saveEditorFile(sessionId, {
          source: entry.source,
          path: entry.path,
          content,
          encoding: current.encoding,
          bom: current.bom,
          newline: current.newline,
          version: current.version,
          force,
        });
        if (generation !== generationRef.current || saved.kind !== "text") return true;
        setDocument(saved);
        documentRef.current = saved;
        if (draftRef.current === content) {
          setDirty(false);
          dirtyRef.current = false;
        }
        setSaveState("saved");
        return true;
      } catch (error) {
        if (generation === generationRef.current) {
          setSaveState(error instanceof ApiError && error.status === 409 ? "conflict" : "failed");
        }
        return false;
      }
    };
    saveQueueRef.current = saveQueueRef.current.then(run, run);
    return saveQueueRef.current;
  }, [sessionId]);

  const confirmDiscard = useCallback(() => new Promise<boolean>((resolve) => {
    modal.confirm({
      title: "文件尚未保存",
      content: "保存失败或文件已在外部修改。是否放弃当前修改？",
      okText: "放弃修改",
      cancelText: "取消",
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    });
  }), [modal]);

  const finishBeforeLeave = useCallback(async (): Promise<boolean> => {
    if (!dirtyRef.current) return true;
    if (await saveNow()) return true;
    return await confirmDiscard();
  }, [confirmDiscard, saveNow]);

  useEffect(() => registerFilePanelCloseGuard(panelWindow.id, finishBeforeLeave), [finishBeforeLeave, panelWindow.id]);

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (!dirtyRef.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, []);

  useEffect(() => {
    if (!dirty || document?.kind !== "text" || saveState === "conflict" || saveState === "failed") return;
    saveTimerRef.current = window.setTimeout(() => { void saveNow(); }, SAVE_DELAY_MS);
    return () => {
      if (saveTimerRef.current !== null) window.clearTimeout(saveTimerRef.current);
    };
  }, [dirty, document, draft, saveNow, saveState]);

  const openFile = useCallback(async (entry: FileTreeEntry, encoding?: string) => {
    if (selectedRef.current?.path !== entry.path && !await finishBeforeLeave()) return;
    const generation = ++generationRef.current;
    setLoadingFile(true);
    try {
      const next = await readEditorFile(sessionId, entry.source, entry.path, encoding);
      if (generation !== generationRef.current) return;
      setSelected(entry);
      selectedRef.current = entry;
      setDocument(next);
      documentRef.current = next;
      const content = next.kind === "text" ? next.content : "";
      setDraft(content);
      draftRef.current = content;
      setDirty(false);
      dirtyRef.current = false;
      setSaveState("idle");
    } catch (error) {
      if (generation === generationRef.current) void message.error(String((error as Error).message ?? error));
    } finally {
      if (generation === generationRef.current) setLoadingFile(false);
    }
  }, [finishBeforeLeave, message, sessionId]);

  const clearSelectionIfAffected = useCallback((source: ManagedFileSource, path: string) => {
    const current = selectedRef.current;
    if (!current || current.source !== source) return;
    if (current.path === path || current.path.startsWith(`${path}/`)) {
      generationRef.current += 1;
      setSelected(null);
      setDocument(null);
      setDraft("");
      setDirty(false);
      dirtyRef.current = false;
      setSaveState("idle");
    }
  }, []);

  const refreshCurrent = useCallback(async () => {
    const entry = selectedRef.current;
    const current = documentRef.current;
    if (!entry || !current) return;
    if (saveStateRef.current === "saving") return;
    try {
      const next = await readEditorFile(sessionId, entry.source, entry.path, current.kind === "text" ? current.encoding : undefined);
      if (next.version === current.version) return;
      if (dirtyRef.current) {
        setSaveState("conflict");
        return;
      }
      setDocument(next);
      documentRef.current = next;
      if (next.kind === "text") {
        setDraft(next.content);
        draftRef.current = next.content;
      }
    } catch (error) {
      const current = selectedRef.current;
      if (current && error instanceof ApiError && error.status === 404) {
        clearSelectionIfAffected(current.source, current.path);
      }
    }
  }, [clearSelectionIfAffected, sessionId]);

  const refreshAll = useCallback(async (notify = false) => {
    try {
      await refreshLoadedDirectories();
      await refreshCurrent();
      if (notify) void message.success("已刷新");
    } catch (error) {
      if (notify) void message.error(String((error as Error).message ?? error));
    }
  }, [message, refreshCurrent, refreshLoadedDirectories]);

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => { void refreshAll(false); }, REFRESH_DELAY_MS);
    return () => window.clearInterval(timer);
  }, [active, refreshAll]);

  const relocateSelectionIfAffected = useCallback((
    oldSource: ManagedFileSource,
    oldPath: string,
    nextSource: ManagedFileSource,
    nextPath: string,
    nextName: string,
  ) => {
    const current = selectedRef.current;
    if (!current || current.source !== oldSource) return;
    if (current.path !== oldPath && !current.path.startsWith(`${oldPath}/`)) return;
    const suffix = current.path.slice(oldPath.length);
    const relocated = {
      ...current,
      source: nextSource,
      path: `${nextPath}${suffix}`,
      name: current.path === oldPath ? nextName : current.name,
    };
    setSelected(relocated);
    selectedRef.current = relocated;
    setDocument((value) => value ? { ...value, source: nextSource, path: relocated.path, name: relocated.name } : value);
    if (documentRef.current) {
      documentRef.current = {
        ...documentRef.current,
        source: nextSource,
        path: relocated.path,
        name: relocated.name,
      };
    }
  }, []);

  const runAction = async () => {
    if (!action?.node.source || !action.node.path) return;
    try {
      if (action.type === "rename") {
        if (!await finishBeforeLeave()) return;
        const moved = await renameFileEntry(sessionId, {
          source: action.node.source,
          path: action.node.path,
          name: actionValue,
        });
        relocateSelectionIfAffected(action.node.source, action.node.path, moved.source, moved.path, moved.name);
      } else {
        await createFileEntry(sessionId, {
          source: action.node.source,
          parent_path: action.node.path,
          name: actionValue,
          kind: action.type === "new-file" ? "file" : "directory",
        });
      }
      setAction(null);
      setActionValue("");
      await refreshAll(false);
    } catch (error) {
      void message.error(String((error as Error).message ?? error));
    }
  };

  const performMove = async () => {
    if (!moveNode?.source || !moveNode.path || !moveTarget) return;
    if (!await finishBeforeLeave()) return;
    try {
      const moved = await moveFileEntry(sessionId, {
        source: moveNode.source,
        path: moveNode.path,
        target_source: moveTarget.source,
        target_parent_path: moveTarget.path,
      });
      relocateSelectionIfAffected(moveNode.source, moveNode.path, moved.source, moved.path, moved.name);
      setMoveNode(null);
      setMoveTarget(null);
      await refreshAll(false);
    } catch (error) {
      void message.error(String((error as Error).message ?? error));
    }
  };

  const menuFor = (node: FileTreeNode): MenuProps => {
    const directory = node.kind === "directory" || node.kind === "root";
    return {
      items: [
        ...(directory ? [
          { key: "new-file", icon: <FileAddOutlined />, label: "新建文件" },
          { key: "new-directory", icon: <FolderAddOutlined />, label: "新建目录" },
          { type: "divider" as const },
        ] : []),
        ...(node.kind !== "root" ? [
          { key: "rename", icon: <EditOutlined />, label: "重命名" },
          { key: "move", icon: <SwapOutlined />, label: "移动到" },
          { key: "delete", icon: <DeleteOutlined />, danger: true, label: "删除" },
        ] : []),
      ],
      onClick: ({ key, domEvent }) => {
        domEvent.stopPropagation();
        if (key === "move") {
          setMoveNode(node);
          return;
        }
        if (key === "delete" && node.source && node.path) {
          const current = selectedRef.current;
          const affectsCurrent = current?.source === node.source
            && (current.path === node.path || current.path.startsWith(`${node.path}/`));
          const wasDirty = affectsCurrent && dirtyRef.current;
          if (affectsCurrent) {
            if (saveTimerRef.current !== null) window.clearTimeout(saveTimerRef.current);
            dirtyRef.current = false;
          }
          void saveQueueRef.current.then(() => recycleFileEntry(sessionId, node.source!, node.path!)).then(() => {
            clearSelectionIfAffected(node.source!, node.path!);
            return refreshAll(false);
          }).catch(() => {
            if (wasDirty) {
              dirtyRef.current = true;
              setDirty(true);
            }
            void message.error("删除失败");
          });
          return;
        }
        setAction({ type: key as "new-file" | "new-directory" | "rename", node });
        setActionValue(key === "rename" ? node.title : "");
      },
    };
  };

  const titleRender = (node: FileTreeNode) => (
    <Dropdown menu={menuFor(node)} trigger={["contextMenu"]} disabled={node.disabled}>
      <span className="file-tree-node" title={node.path ?? node.title}>
        <span>{node.title}</span>
        {!node.disabled ? <MoreOutlined className="file-tree-node-more" /> : null}
      </span>
    </Dropdown>
  );

  const saveLabel = useMemo(() => ({
    idle: "",
    saving: "保存中",
    saved: "已保存",
    failed: "保存失败",
    conflict: "文件已在外部修改",
  })[saveState], [saveState]);

  const editorContent = () => {
    if (loadingFile) return <Spin />;
    if (!document || !selected) return <Empty description="从右侧文件树选择文件" />;
    if (document.kind === "text") {
      return (
        <Suspense fallback={<Spin />}>
          <CodeEditor
            filename={document.name}
            newline={document.newline}
            value={draft}
            onChange={(value) => {
              setDraft(value);
              draftRef.current = value;
              setDirty(true);
              dirtyRef.current = true;
              if (saveState !== "conflict") setSaveState("idle");
            }}
          />
        </Suspense>
      );
    }
    if (document.kind === "image") {
      return <img className="file-image-preview" src={sessionFileContentUrl(sessionId, selected.source, selected.path)} alt={selected.name} />;
    }
    if (document.kind === "encoding_required") {
      return (
        <Space direction="vertical" align="center">
          <Typography.Text>请选择文字编码</Typography.Text>
          <Select
            aria-label="文字编码"
            style={{ width: 180 }}
            options={document.encodings.map((value) => ({ value, label: value.toUpperCase() }))}
            onChange={(value) => void openFile(selected, value)}
          />
        </Space>
      );
    }
    return (
      <Space direction="vertical" align="center">
        <Typography.Text>{document.kind === "too_large" ? "文件超过 5 MB，不能打开" : "此文件仅支持下载"}</Typography.Text>
        <Button icon={<DownloadOutlined />} href={sessionFileContentUrl(sessionId, selected.source, selected.path, true)}>下载</Button>
      </Space>
    );
  };

  const contentPanel = (
    <div className="file-preview-pane">
      <div className="file-preview-toolbar">
        <Typography.Text ellipsis title={selected?.path}>{selected?.path ?? "未选择文件"}</Typography.Text>
        <Space size={4}>
          {saveLabel ? <Typography.Text type={saveState === "failed" || saveState === "conflict" ? "danger" : "secondary"}>{saveLabel}</Typography.Text> : null}
          {saveState === "failed" ? <Tooltip title="重试保存"><Button type="text" size="small" icon={<ReloadOutlined />} onClick={() => void saveNow()} /></Tooltip> : null}
          {selected ? <Tooltip title="下载"><Button type="text" size="small" icon={<DownloadOutlined />} href={sessionFileContentUrl(sessionId, selected.source, selected.path, true)} /></Tooltip> : null}
          <Tooltip title={treeCollapsed ? "展开文件树" : "收起文件树"}>
            <Button
              type="text"
              size="small"
              aria-label={treeCollapsed ? "展开文件树" : "收起文件树"}
              icon={treeCollapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              onClick={() => setTreeCollapsed((value) => !value)}
            />
          </Tooltip>
        </Space>
      </div>
      {saveState === "conflict" && document?.kind === "text" ? (
        <Alert
          banner
          type="warning"
          message="文件已在外部修改"
          action={<Space><Button size="small" onClick={() => {
            setDirty(false);
            dirtyRef.current = false;
            void openFile(selected!);
          }}>重新加载</Button><Button size="small" type="primary" onClick={() => void saveNow(true)}>覆盖</Button></Space>}
        />
      ) : null}
      <div className="file-preview-content">{editorContent()}</div>
    </div>
  );

  const treePanel = (
    <div className="file-tree-pane">
      <div className="file-tree-toolbar">
        <Typography.Text strong>文件</Typography.Text>
        <Space size={4}>
          <Tooltip title="刷新"><Button type="text" size="small" aria-label="刷新文件树" icon={<ReloadOutlined />} onClick={() => void refreshAll(true)} /></Tooltip>
          <Tooltip title="收起文件树"><Button type="text" size="small" aria-label="收起文件树" icon={<MenuFoldOutlined />} onClick={() => setTreeCollapsed(true)} /></Tooltip>
        </Space>
      </div>
      <Tree<FileTreeNode>
        aria-label="文件树"
        blockNode
        expandedKeys={expandedKeys}
        loadedKeys={loadedKeys}
        loadData={loadNode}
        selectedKeys={selected ? [`${selected.source}:${selected.path}`] : []}
        showIcon
        titleRender={titleRender}
        treeData={treeNodes}
        onExpand={(keys) => setExpandedKeys(keys)}
        onLoad={(keys) => setLoadedKeys(keys)}
        onSelect={(_keys, info) => {
          const entry = info.node.entry;
          if (entry?.kind === "file") {
            void openFile(entry).then(() => {
              if (isMobile) setTreeCollapsed(true);
            });
          }
        }}
      />
    </div>
  );

  return (
    <div className="right-panel-files">
      {treeCollapsed ? contentPanel : isMobile ? treePanel : (
        <Splitter onResize={(sizes) => setTreeWidth(Number(sizes[1]) || treeWidth)}>
          <Splitter.Panel min={220}>{contentPanel}</Splitter.Panel>
          <Splitter.Panel size={treeWidth} min={180} max="55%">{treePanel}</Splitter.Panel>
        </Splitter>
      )}
      <Modal
        title={action?.type === "rename" ? "重命名" : action?.type === "new-directory" ? "新建目录" : "新建文件"}
        open={Boolean(action)}
        okButtonProps={{ disabled: !actionValue.trim() }}
        onCancel={() => setAction(null)}
        onOk={() => void runAction()}
      >
        <Input autoFocus aria-label="名称" value={actionValue} onChange={(event) => setActionValue(event.target.value)} onPressEnter={() => void runAction()} />
      </Modal>
      <Modal
        title="移动到"
        open={Boolean(moveNode)}
        okButtonProps={{ disabled: !moveTarget }}
        onCancel={() => { setMoveNode(null); setMoveTarget(null); }}
        onOk={() => void performMove()}
      >
        <DirectoryPicker
          sessionId={sessionId}
          selected={moveTarget ? `${moveTarget.source}:${moveTarget.path}` : null}
          onSelect={(source, path) => setMoveTarget({ source, path })}
        />
      </Modal>
    </div>
  );
}
