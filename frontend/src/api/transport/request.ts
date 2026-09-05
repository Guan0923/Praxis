/** Shared browser request helpers used by all API domains. */

import { apiUrl } from "./base";
import { type OperationTarget, windowOperationControl } from "./operationControl";

export class ApiError extends Error {
  constructor(public readonly status: number, message: string, public readonly code?: string) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ApiErrorDetails {
  message: string;
  code?: string;
}

export async function errorDetailsFrom(res: Response): Promise<ApiErrorDetails> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") {
      return {
        message: body.detail,
        code: typeof body.code === "string" ? body.code : undefined,
      };
    }
  } catch {
    /* fall through */
  }
  return { message: `HTTP ${res.status}` };
}

export async function errorFrom(res: Response): Promise<string> {
  return (await errorDetailsFrom(res)).message;
}

export interface OperationRequestInit extends RequestInit {
  operation?: OperationTarget | false;
}

export async function requestRaw(url: string, init: OperationRequestInit = {}): Promise<Response> {
  const { operation, ...requestInit } = init;
  const resolved = { cache: "no-store" as RequestCache, ...requestInit };
  const method = (resolved.method ?? "GET").toUpperCase();
  const res = operation === false || !["POST", "PUT", "PATCH", "DELETE"].includes(method)
    ? await fetch(apiUrl(url), resolved)
    : await windowOperationControl.request(url, resolved, operation ?? {});
  if (!res.ok) {
    const details = await errorDetailsFrom(res);
    throw new ApiError(res.status, details.message, details.code);
  }
  return res;
}

export async function requestJson<T>(url: string, init: OperationRequestInit = {}): Promise<T> {
  const res = await requestRaw(url, init);
  return res.json() as Promise<T>;
}

export async function requestOptionalJson<T>(url: string, init: OperationRequestInit = {}): Promise<T | null> {
  const res = await requestRaw(url, init);
  return res.status === 204 ? null : res.json() as Promise<T>;
}

export async function requestVoid(url: string, init: OperationRequestInit = {}): Promise<void> {
  await requestRaw(url, init);
}

export function jsonBody(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
