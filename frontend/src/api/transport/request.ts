/** Shared browser request helpers used by all API domains. */

import { errorSummary, readErrorReport, type ErrorReport } from "../errorReport";
import { apiUrl } from "./base";
import { type OperationTarget, windowOperationControl } from "./operationControl";

export class ApiError extends Error {
  constructor(public readonly status: number, message: string, public readonly code?: string, public readonly error_report?: ErrorReport) {
    super(error_report ? errorSummary({ error_report }) : message);
    this.name = "ApiError";
  }
}

export interface ApiErrorDetails {
  message: string;
  code?: string;
  error_report?: ErrorReport;
}

export async function errorDetailsFrom(res: Response): Promise<ApiErrorDetails> {
  try {
    const body = await res.json();
    const report = readErrorReport(body?.error_report);
    if (report || (body && typeof body.detail === "string")) {
      return {
        message: typeof body.detail === "string" ? body.detail : report!.message,
        error_report: report,
        code: typeof body.code === "string" ? body.code : undefined,
      };
    }
  } catch {
    /* fall through */
  }
  return { message: `HTTP ${res.status}` };
}

export async function apiErrorFrom(res: Response): Promise<ApiError> {
  const details = await errorDetailsFrom(res);
  return new ApiError(res.status, details.message, details.code, details.error_report);
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
    throw new ApiError(res.status, details.message, details.code, details.error_report);
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
