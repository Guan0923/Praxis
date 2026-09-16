import { ErrorDisplay } from "../../components/ErrorDisplay";
import { LeftOutlined, ReloadOutlined, RightOutlined } from "@ant-design/icons";
import { Button, Input } from "antd";
import type { TextAreaRef } from "antd/es/input/TextArea";
import type { MouseEvent as ReactMouseEvent, RefObject, UIEventHandler, WheelEventHandler } from "react";
import MarkdownContent from "../../components/MarkdownContent";
import ShimmerText from "../../components/ShimmerText";
import type { ChatMessage, DecisionRequest, DisplayMode } from "../../types";
import { conversationTurnId } from "./ConversationTimeline";
import { AssistantMessage, MessageActions, MessageReferenceChip } from "./messageParts";
import { ChatViewportProvider, VirtualBlock } from "./ChatViewport";
import { useLatestCallback } from "./useLatestCallback";
import { memo } from "react";

interface ChatMessageListProps {
  messages: ChatMessage[];
  sessionId?: string;
  threadId?: string;
  display: DisplayMode;
  interactionBusy: boolean;
  compactionPending: boolean;
  chatScrollRef: RefObject<HTMLDivElement>;
  onScroll: UIEventHandler<HTMLDivElement>;
  onWheel?: WheelEventHandler<HTMLDivElement>;
  editingMessageId: string | null;
  editingDraft: string;
  editRef: RefObject<TextAreaRef>;
  rewindPending: boolean;
  editingSubmitting: boolean;
  canEdit: boolean;
  setEditingDraft: (value: string) => void;
  cancelEdit: () => void;
  saveEdit: (message: ChatMessage) => Promise<void>;
  beginEdit: (message: ChatMessage) => void;
  handleUserBubbleClick: (event: ReactMouseEvent<HTMLDivElement>, message: ChatMessage) => void;
  messageVersion: (message: ChatMessage) => { index: number; total: number; busy?: boolean } | undefined;
  changeMessageVersion: (message: ChatMessage, direction: -1 | 1) => Promise<void>;
  onDecision: (request: DecisionRequest, choice: string, options?: { supplement?: string; answers?: Record<string, string[]> }) => Promise<void>;
  onFork?: (messageId: string) => void;
  sandboxFailure?: string | null;
  sandboxErrorReport?: unknown;
  onRetrySend?: (message: ChatMessage) => void;
}

export function ChatMessageList(props: ChatMessageListProps) {
  const onDecision = useLatestCallback(props.onDecision);
  const onFork = useLatestCallback((id: string) => props.onFork?.(id));
  const beginEdit = useLatestCallback(props.beginEdit);
  const saveEdit = useLatestCallback(props.saveEdit);
  const cancelEdit = useLatestCallback(props.cancelEdit);
  const changeMessageVersion = useLatestCallback(props.changeMessageVersion);
  const handleUserBubbleClick = useLatestCallback(props.handleUserBubbleClick);
  const setEditingDraft = useLatestCallback(props.setEditingDraft);
  const onRetrySend = useLatestCallback((message: ChatMessage) => props.onRetrySend?.(message));
  return <MessageList {...props} onDecision={onDecision} onFork={props.onFork ? onFork : undefined}
    beginEdit={beginEdit} saveEdit={saveEdit} cancelEdit={cancelEdit} changeMessageVersion={changeMessageVersion}
    handleUserBubbleClick={handleUserBubbleClick} setEditingDraft={setEditingDraft} onRetrySend={onRetrySend} />;
}

function MessageList({
  messages,
  sessionId,
  threadId,
  display,
  interactionBusy,
  compactionPending,
  chatScrollRef,
  onScroll,
  onWheel,
  editingMessageId,
  editingDraft,
  editRef,
  rewindPending,
  editingSubmitting,
  canEdit,
  setEditingDraft,
  cancelEdit,
  saveEdit,
  beginEdit,
  handleUserBubbleClick,
  messageVersion,
  changeMessageVersion,
  onDecision,
  onFork,
  sandboxFailure,
  sandboxErrorReport,
  onRetrySend,
}: ChatMessageListProps) {
  return (
    <div className="chat-scroll" style={{ overflowAnchor: "none" }} ref={chatScrollRef} data-conversation-scroll onScroll={onScroll} onWheel={onWheel}>
      <ChatViewportProvider key={sessionId + "/" + threadId} scrollRef={chatScrollRef}>
      <div className="chat-scroll-content">
        <div className="chat-messages">
          {messages.length === 0 ? (
            <div className="welcome">
              <div className="logo">Praxis</div>
              <p className="welcome-sub">向你的智能体提问，它会调用文件、Shell、Web 等工具完成任务</p>
            </div>
          ) : messages.map((message) => {
            const version = message.role === "user" ? messageVersion(message) : undefined;
            return message.role === "user" ? (
            <UserMessage key={message.id} message={message} sessionId={sessionId} interactionBusy={interactionBusy}
              editingMessageId={editingMessageId === message.id ? editingMessageId : null}
              editingDraft={editingMessageId === message.id ? editingDraft : ""} editRef={editRef}
              rewindPending={rewindPending} editingSubmitting={editingSubmitting} canEdit={canEdit}
              setEditingDraft={setEditingDraft} cancelEdit={cancelEdit} saveEdit={saveEdit} beginEdit={beginEdit}
              handleUserBubbleClick={handleUserBubbleClick} changeMessageVersion={changeMessageVersion}
              versionIndex={version?.index} versionTotal={version?.total}
              versionBusy={version?.busy} onRetrySend={onRetrySend} />
          ) : (
            <AssistantMessage
              key={message.id}
              sessionId={sessionId}
              threadId={threadId}
              msg={message}
              display={display}
              onDecision={onDecision}
              busy={interactionBusy}
              onFork={onFork && (message.status === "success" || message.status === "failed" || message.status === "paused") ? onFork : undefined}
            />
          ); })}
          {sandboxFailure ? (
            <div className="message assistant sandbox-health-failure" role="status" aria-live="polite">
              <div className="message-content">
                <div className="bubble assistant-bubble">
                  <ErrorDisplay error={sandboxFailure} report={sandboxErrorReport} />
                </div>
              </div>
            </div>
          ) : null}
          {compactionPending ? (
            <div className="message assistant runtime-compaction-progress" role="status" aria-live="polite">
              <ShimmerText active>正在执行compaction操作中</ShimmerText>
            </div>
          ) : null}
        </div>
      </div>
      </ChatViewportProvider>
    </div>
  );
}

type UserMessageProps = Pick<ChatMessageListProps,
  "sessionId" | "interactionBusy" | "editingMessageId" | "editingDraft" | "editRef" | "rewindPending"
  | "editingSubmitting" | "canEdit" | "setEditingDraft" | "cancelEdit" | "saveEdit" | "beginEdit"
  | "handleUserBubbleClick" | "changeMessageVersion" | "onRetrySend"
> & { message: ChatMessage; versionIndex?: number; versionTotal?: number; versionBusy?: boolean };

const UserMessage = memo(function UserMessage({
  message, sessionId, interactionBusy, editingMessageId, editingDraft, editRef, rewindPending,
  editingSubmitting, canEdit, setEditingDraft, cancelEdit, saveEdit, beginEdit, handleUserBubbleClick,
  changeMessageVersion, onRetrySend, versionIndex, versionTotal, versionBusy,
}: UserMessageProps) {
  const messageVersion = () => versionIndex === undefined || versionTotal === undefined ? undefined : { index: versionIndex, total: versionTotal, busy: versionBusy };
  return (
    <div className={`message user${message.pending ? " is-pending" : ""}`} id={conversationTurnId(message.id)} data-chat-anchor-key={message.id} data-scroll-message-id={message.id} key={message.id}>
      <VirtualBlock id={message.id + ":user:" + (versionIndex ?? 0)} revision={message.content} estimate={Math.max(90, Math.ceil(message.content.length / 65) * 22 + 60)} pinned={editingMessageId === message.id || Boolean(message.pending)}>
      <div className={editingMessageId === message.id ? "message-content is-editing" : "message-content"}>
        {editingMessageId === message.id ? (
          <div className="message-edit" aria-label="编辑用户消息">
            <Input.TextArea
              className="message-edit-input"
              ref={editRef}
              aria-label="编辑用户消息"
              value={editingDraft}
              disabled={interactionBusy}
              onChange={(event) => setEditingDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  cancelEdit();
                } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                  event.preventDefault();
                  void saveEdit(message);
                }
              }}
              autoSize={{ minRows: 2, maxRows: 8 }}
            />
            <div className="message-edit-actions">
              <Button type="text" onClick={cancelEdit}>取消</Button>
              <Button
                type="primary"
                onClick={() => void saveEdit(message)}
                loading={rewindPending || editingSubmitting}
                disabled={!editingDraft.trim() || editingSubmitting || rewindPending || interactionBusy}
              >保存并重新生成</Button>
            </div>
          </div>
        ) : (
          <div
            className="bubble user-bubble"
            onClick={(event) => handleUserBubbleClick(event, message)}
            title={canEdit && !interactionBusy ? "点击编辑此消息" : undefined}
          >
            {message.approvedPlanHandoff ? <span className="plan-handoff-summary">已批准计划，开始实施</span> : <MarkdownContent text={message.content} />}
            {message.references && message.references.length > 0 ? (
              <div className="message-references" aria-label="消息引用">
                {message.references.map((reference) => (
                  <MessageReferenceChip key={`${reference.source}:${reference.path}`} reference={reference} sessionId={sessionId} />
                ))}
              </div>
            ) : null}
            {message.pending ? <span className="agent-message-pending" role="status">发送中…</span> : null}
            {message.error ? (
              <span role="alert">
                <ErrorDisplay error={message.error} report={message.error_report} />
                <Button type="text" size="small" icon={<ReloadOutlined />} title="重试发送" aria-label="重试发送" disabled={interactionBusy} onClick={(event) => { event.stopPropagation(); onRetrySend?.(message); }} />
              </span>
            ) : null}
          </div>
        )}
        {editingMessageId !== message.id ? (
          <MessageTools
            message={message}
            interactionBusy={interactionBusy}
            canEdit={canEdit}
            beginEdit={beginEdit}
            messageVersion={messageVersion}
            changeMessageVersion={changeMessageVersion}
          />
        ) : null}
      </div>
      </VirtualBlock>
    </div>
  );
});

function MessageTools({
  message,
  interactionBusy,
  canEdit,
  beginEdit,
  messageVersion,
  changeMessageVersion,
}: {
  message: ChatMessage;
  interactionBusy: boolean;
  canEdit: boolean;
  beginEdit: (message: ChatMessage) => void;
  messageVersion: (message: ChatMessage) => { index: number; total: number; busy?: boolean } | undefined;
  changeMessageVersion: (message: ChatMessage, direction: -1 | 1) => Promise<void>;
}) {
  const version = messageVersion(message);
  return (
    <>
      <MessageActions msg={message} busy={interactionBusy} onEdit={canEdit ? () => beginEdit(message) : undefined} />
      {version ? (
        <div className="message-version-controls" aria-label="消息版本切换">
          <Button
            type="text"
            size="small"
            icon={<LeftOutlined />}
            aria-label="上一个消息版本"
            disabled={version.busy || version.index === 0}
            onClick={() => void changeMessageVersion(message, -1)}
          />
          <span aria-live="polite">{version.index + 1} / {version.total}</span>
          <Button
            type="text"
            size="small"
            icon={<RightOutlined />}
            aria-label="下一个消息版本"
            disabled={version.busy || version.index >= version.total - 1}
            onClick={() => void changeMessageVersion(message, 1)}
          />
        </div>
      ) : null}
    </>
  );
}
