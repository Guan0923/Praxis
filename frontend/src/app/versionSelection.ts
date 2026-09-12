import { patchTurnCurrentData, type VersionSelection } from "../api/conversations/turns";
import { recordVersionChoice } from "../api/conversations/versionClock";
import { isRuntimeTurnNode } from "./runtime/runtimeNodeNormalization";
import { projectTurnPath } from "./runtime/runtimeDetailProjection";
import type { Conversation } from "../types";

type Update = (id: string, updater: (current: Conversation) => Conversation) => void;
interface Pending { desired: number; confirmed: number; generation: number; revision: number; saving: boolean }
const pending = new Map<string, Pending>();
const revisions = new Map<string, number>();
const keyOf = (session: string, turn: string) => `${session}:${turn}`;

export function displayVersion(current: Conversation, turnId: string, index: number): Conversation {
  const turn = current.runtimeNodes?.find((node) => node.id === turnId);
  if (!turn || !isRuntimeTurnNode(turn) || !turn.data[index] || turn.current_data_idx === index) return current;
  const updated = { ...turn, current_data_idx: index };
  const replacement = projectTurnPath(new Map([[keyOf(turn.session_id, turnId), updated]]), turnId, true);
  const prefix = `${turnId}:message:`;
  const first = current.messages.findIndex((message) => message.id.startsWith(prefix));
  const messages = first < 0 ? current.messages : [
    ...current.messages.slice(0, first), ...replacement,
    ...current.messages.slice(first).filter((message) => !message.id.startsWith(prefix)),
  ];
  let historyCursor = current.historyCursor;
  if (historyCursor) {
    const cursor = JSON.parse(historyCursor);
    if (cursor.head === turnId) historyCursor = JSON.stringify({ ...cursor, version: index });
  }
  return { ...current, historyCursor, runtimeNodes: current.runtimeNodes!.map((node) => node === turn ? updated : node), messages };
}

export function receiveVersion(selection: VersionSelection, update: Update, conversationId: string) {
  const key = keyOf(selection.session_id, selection.id);
  if ((revisions.get(key) ?? -1) > selection.revision) return;
  revisions.set(key, selection.revision);
  const operation = pending.get(key);
  if (operation) {
    operation.confirmed = selection.current_data_idx;
    operation.revision = selection.revision;
  }
  update(conversationId, (current) => displayVersion(current, selection.id, operation?.desired ?? selection.current_data_idx));
  recordVersionChoice(selection.session_id, selection.id, operation?.desired ?? selection.current_data_idx);
}

export function overlayPendingVersions(current: Conversation) {
  let result = current;
  for (const node of current.runtimeNodes ?? []) {
    const operation = pending.get(keyOf(node.session_id, node.id));
    if (operation) result = displayVersion(result, node.id, operation.desired);
  }
  return result;
}

export async function selectVersion(conversation: Conversation, turnId: string, direction: -1 | 1, update: Update, onError: (error: unknown) => void) {
  const turn = conversation.runtimeNodes?.find((node) => node.id === turnId);
  if (!turn || !isRuntimeTurnNode(turn) || turn.status === "running") return;
  const key = keyOf(turn.session_id, turnId);
  let operation = pending.get(key);
  const desired = (operation?.desired ?? turn.current_data_idx) + direction;
  if (desired < 0 || desired >= turn.data.length) return;
  if (!operation) {
    operation = { desired, confirmed: turn.current_data_idx, generation: 0, revision: revisions.get(key) ?? 0, saving: false };
    pending.set(key, operation);
  }
  operation.desired = desired;
  recordVersionChoice(turn.session_id, turnId, desired);
  operation.generation += 1;
  update(conversation.id, (current) => displayVersion(current, turnId, desired));
  if (operation.saving) return;
  operation.saving = true;
  try {
    while (true) {
      const generation = operation.generation;
      const target = operation.desired;
      try {
        const response = await patchTurnCurrentData(turnId, target, turn.session_id);
        receiveVersion(response, update, conversation.id);
      } catch (error) {
        onError(error);
        if (generation === operation.generation) {
          operation.desired = operation.confirmed;
          recordVersionChoice(turn.session_id, turnId, operation.confirmed);
          update(conversation.id, (current) => displayVersion(current, turnId, operation!.confirmed));
          return;
        }
      }
      if (generation === operation.generation || operation.desired === operation.confirmed) {
        operation.desired = operation.confirmed;
        recordVersionChoice(turn.session_id, turnId, operation.confirmed);
        update(conversation.id, (current) => displayVersion(current, turnId, operation!.confirmed));
        return;
      }
    }
  } finally { pending.delete(key); }
}
