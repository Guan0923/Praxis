import { apiUrl } from "./base";

export type SessionOwnership = "unknown" | "writable" | "readonly";

export interface OperationTarget {
  group?: string;
  sessionId?: string;
  dedupeKey?: string;
}

export type OperationSender = (url: string, init: RequestInit) => Promise<Response>;

interface GroupState {
  seq: number;
  ack: number;
  uncertain: boolean;
}

interface ControlReply {
  type?: string;
  request_id?: string;
  token?: string;
  generation?: number;
  session_id?: string;
  writable?: boolean;
  group?: string;
  seq?: number;
  ack?: number;
}

const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export class OperationPendingError extends Error {
  constructor() {
    super("该操作正在处理中。");
    this.name = "OperationPendingError";
  }
}

class WindowOperationControl {
  private windowId = crypto.randomUUID();
  private token: string | null = null;
  private generation: number | null = null;
  private socket: WebSocket | null = null;
  private connectPromise: Promise<void> | null = null;
  private reconnectTimer: number | undefined;
  private rpc = new Map<string, { resolve: (value: ControlReply) => void; reject: (error: Error) => void }>();
  private groups = new Map<string, GroupState>();
  private tails = new Map<string, Promise<void>>();
  private deduplicated = new Map<string, Promise<Response>>();
  private ownership = new Map<string, SessionOwnership>();
  private claimedSessions = new Set<string>();
  private ownershipListeners = new Set<() => void>();
  private threadSessions = new Map<string, string>();
  private turnSessions = new Map<string, string>();

  id(): string {
    return this.windowId;
  }

  currentGeneration(): number | null {
    return this.generation;
  }

  bindSessionResources(sessionId: string, threadIds: string[], turnIds: string[]): void {
    threadIds.forEach((id) => this.threadSessions.set(id, sessionId));
    turnIds.forEach((id) => this.turnSessions.set(id, sessionId));
  }

  subscribeOwnership(listener: () => void): () => void {
    this.ownershipListeners.add(listener);
    return () => this.ownershipListeners.delete(listener);
  }

  ownershipFor(sessionId?: string): SessionOwnership {
    if (import.meta.env.MODE === "test") return "writable";
    return sessionId ? this.ownership.get(sessionId) ?? "unknown" : "writable";
  }

  async claimSession(sessionId: string): Promise<SessionOwnership> {
    if (import.meta.env.MODE === "test") return "writable";
    this.claimedSessions.add(sessionId);
    await this.ensureConnected();
    const reply = await this.sendRpc({ type: "session.claim", session_id: sessionId });
    const status: SessionOwnership = reply.writable ? "writable" : "readonly";
    this.setOwnership(sessionId, status);
    return status;
  }

  async request(url: string, init: RequestInit, target: OperationTarget = {}): Promise<Response> {
    return this.requestUsing(url, init, target, (targetUrl, targetInit) => fetch(apiUrl(targetUrl), targetInit));
  }

  async requestUsing(
    url: string,
    init: RequestInit,
    target: OperationTarget,
    sender: OperationSender,
  ): Promise<Response> {
    if (import.meta.env.MODE === "test") return sender(url, init);
    const method = (init.method ?? "GET").toUpperCase();
    if (!WRITE_METHODS.has(method)) return sender(url, init);
    const inferred = this.inferTarget(url, init, target);
    const dedupeKey = inferred.dedupeKey ?? `${inferred.group}:${method}:${new URL(apiUrl(url), window.location.origin).pathname}`;
    const existing = this.deduplicated.get(dedupeKey);
    if (existing) throw new OperationPendingError();
    const previous = this.tails.get(inferred.group) ?? Promise.resolve();
    const operation = previous.catch(() => undefined).then(() => this.perform(url, init, inferred, sender));
    this.deduplicated.set(dedupeKey, operation);
    const tail = operation.then(() => undefined, () => undefined);
    this.tails.set(inferred.group, tail);
    const cleanup = () => {
      if (this.deduplicated.get(dedupeKey) === operation) this.deduplicated.delete(dedupeKey);
      if (this.tails.get(inferred.group) === tail) this.tails.delete(inferred.group);
    };
    void operation.then(cleanup, cleanup);
    return operation;
  }

  private async perform(
    url: string,
    init: RequestInit,
    target: Required<Pick<OperationTarget, "group">> & OperationTarget,
    sender: OperationSender,
  ): Promise<Response> {
    await this.ensureConnected();
    if (target.sessionId) {
      const ownership = this.ownership.get(target.sessionId) === "writable"
        ? "writable" : await this.claimSession(target.sessionId);
      if (ownership !== "writable") throw new Error("当前 session 正在另一个窗口对话。");
    }
    const path = new URL(apiUrl(url), window.location.origin).pathname;
    const retryable = target.group.startsWith("turn-control:") || path === "/api/turns" || path.includes("/queued-messages");
    if (retryable && this.groups.get(target.group)?.uncertain) this.groups.delete(target.group);
    const state = await this.openGroup(target.group);
    if (state.uncertain) throw new Error("上一次操作结果不明确，请刷新页面核对后再继续。");
    const headers = new Headers(init.headers);
    headers.set("X-Praxis-Window", this.windowId);
    headers.set("X-Praxis-Window-Generation", String(this.generation));
    headers.set("X-Praxis-Operation-Group", target.group);
    headers.set("X-Praxis-Seq", String(state.seq));
    headers.set("X-Praxis-Ack", String(state.ack));
    if (target.sessionId) headers.set("X-Praxis-Session", target.sessionId);
    let response: Response;
    try {
      response = await sender(url, { ...init, headers });
    } catch {
      state.uncertain = true;
      throw new Error("操作响应丢失，结果不明确，请刷新页面核对后再继续。");
    }
    if (response.status === 423 && target.sessionId) {
      this.setOwnership(target.sessionId, "readonly");
      return response;
    }
    const responseSeq = Number(response.headers.get("X-Praxis-Seq"));
    const responseAck = Number(response.headers.get("X-Praxis-Ack"));
    if (!Number.isSafeInteger(responseSeq) || !Number.isSafeInteger(responseAck)) {
      state.uncertain = true;
      throw new Error("操作确认响应无效，请刷新页面核对。");
    }
    if (response.status === 409) {
      const body = await response.clone().json().catch(() => null) as { code?: string } | null;
      if (body?.code === "operation_sequence_mismatch") state.uncertain = true;
    }
    state.seq = responseAck;
    state.ack = responseSeq + 1;
    return response;
  }

  private inferTarget(url: string, init: RequestInit, target: OperationTarget): Required<Pick<OperationTarget, "group">> & OperationTarget {
    const path = new URL(apiUrl(url), window.location.origin).pathname;
    let sessionId = target.sessionId;
    const sessionMatch = path.match(/^\/api\/sessions\/([^/]+)/) ?? path.match(/^\/api\/right-panel\/([^/]+)/);
    if (!sessionId && sessionMatch) sessionId = decodeURIComponent(sessionMatch[1]);
    const threadMatch = path.match(/^\/api\/(?:sidebar-threads|agent-threads)\/([^/]+)/);
    if (!sessionId && threadMatch) sessionId = this.threadSessions.get(decodeURIComponent(threadMatch[1]));
    const turnMatch = path.match(/^\/api\/turns\/([^/]+)/);
    if (!sessionId && turnMatch) sessionId = this.turnSessions.get(decodeURIComponent(turnMatch[1]));
    if (!sessionId && typeof init.body === "string") {
      try {
        const body = JSON.parse(init.body) as { session_id?: unknown; thread_id?: unknown; id?: unknown };
        if (typeof body.session_id === "string") sessionId = body.session_id;
        if (sessionId && typeof body.thread_id === "string") this.threadSessions.set(body.thread_id, sessionId);
        if (sessionId && typeof body.id === "string") this.turnSessions.set(body.id, sessionId);
      } catch {
        // Non-JSON writes such as FormData are scoped by their URL.
      }
    }
    let group = target.group;
    if (!group && sessionId) group = `session:${sessionId}`;
    if (!group) {
      const project = path.match(/^\/api\/projects\/([^/]+)/);
      if (project) group = `project:${decodeURIComponent(project[1])}`;
      else if (path === "/api/projects") group = "projects:create";
      else if (path.startsWith("/api/settings/")) group = `settings:${path.split("/")[3] ?? "general"}`;
      else if (path.startsWith("/benchmark/")) group = "benchmark";
      else group = `resource:${path}`;
    }
    return { ...target, group, sessionId };
  }

  private async openGroup(group: string): Promise<GroupState> {
    const existing = this.groups.get(group);
    if (existing) return existing;
    const reply = await this.sendRpc({ type: "group.open", group });
    if (!Number.isSafeInteger(reply.seq) || !Number.isSafeInteger(reply.ack)) {
      throw new Error("操作序号握手失败。");
    }
    const state = { seq: reply.seq!, ack: reply.ack!, uncertain: false };
    this.groups.set(group, state);
    return state;
  }

  private ensureConnected(): Promise<void> {
    if (this.socket?.readyState === WebSocket.OPEN) return Promise.resolve();
    if (this.connectPromise) return this.connectPromise;
    this.connectPromise = new Promise<void>((resolve, reject) => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const params = new URLSearchParams({ window_id: this.windowId });
      if (this.token) params.set("token", this.token);
      const socket = new WebSocket(`${scheme}://${window.location.host}/api/window-control/ws?${params}`);
      this.socket = socket;
      let ready = false;
      socket.onmessage = (event) => {
        let message: ControlReply;
        try {
          message = JSON.parse(String(event.data)) as ControlReply;
        } catch {
          return;
        }
        if (message.type === "ready" && message.token) {
          const backendRestarted = this.token !== null && this.token !== message.token;
          this.token = message.token;
          this.generation = Number.isSafeInteger(message.generation) ? message.generation! : null;
          if (this.generation === null) {
            socket.close();
            return;
          }
          if (backendRestarted) this.groups.clear();
          ready = true;
          this.connectPromise = null;
          resolve();
          for (const sessionId of this.claimedSessions) void this.claimSession(sessionId);
          return;
        }
        if (message.type === "session.ownership" && message.session_id) {
          this.setOwnership(message.session_id, message.writable ? "writable" : "readonly");
        }
        if (message.request_id) {
          const pending = this.rpc.get(message.request_id);
          if (pending) {
            this.rpc.delete(message.request_id);
            pending.resolve(message);
          }
        }
      };
      socket.onclose = () => {
        this.socket = null;
        this.connectPromise = null;
        for (const sessionId of this.claimedSessions) this.setOwnership(sessionId, "unknown");
        const error = new Error("窗口连接已断开。");
        for (const pending of this.rpc.values()) pending.reject(error);
        this.rpc.clear();
        if (!ready) reject(error);
        if (this.reconnectTimer === undefined) {
          this.reconnectTimer = window.setTimeout(() => {
            this.reconnectTimer = undefined;
            void this.ensureConnected().catch(() => undefined);
          }, 500);
        }
      };
      socket.onerror = () => undefined;
    });
    return this.connectPromise;
  }

  private async sendRpc(payload: Record<string, unknown>): Promise<ControlReply> {
    await this.ensureConnected();
    const requestId = crypto.randomUUID();
    return new Promise<ControlReply>((resolve, reject) => {
      this.rpc.set(requestId, { resolve, reject });
      this.socket!.send(JSON.stringify({ ...payload, request_id: requestId }));
    });
  }

  private setOwnership(sessionId: string, value: SessionOwnership): void {
    if (this.ownership.get(sessionId) === value) return;
    this.ownership.set(sessionId, value);
    this.ownershipListeners.forEach((listener) => listener());
  }
}

export const windowOperationControl = new WindowOperationControl();

export function browserWindowId(): string {
  return windowOperationControl.id();
}

export function browserWindowGeneration(): number | null {
  return windowOperationControl.currentGeneration();
}

export function bindSessionOperationResources(sessionId: string, threadIds: string[], turnIds: string[]): void {
  windowOperationControl.bindSessionResources(sessionId, threadIds, turnIds);
}

export function claimSessionOwnership(sessionId: string): Promise<SessionOwnership> {
  return windowOperationControl.claimSession(sessionId);
}

export function sessionOwnership(sessionId?: string): SessionOwnership {
  return windowOperationControl.ownershipFor(sessionId);
}

export function subscribeSessionOwnership(listener: () => void): () => void {
  return windowOperationControl.subscribeOwnership(listener);
}
