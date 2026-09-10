export interface ErrorReport {
  type: string;
  message: string;
  traceback: string;
  errno?: number;
  winerror?: number;
}

export function readErrorReport(value: unknown): ErrorReport | undefined {
  if (!value || typeof value !== "object") return undefined;
  const report = value as Record<string, unknown>;
  if (typeof report.type !== "string" || typeof report.message !== "string" || typeof report.traceback !== "string") return undefined;
  return { type: report.type, message: report.message, traceback: report.traceback,
    ...(typeof report.errno === "number" ? { errno: report.errno } : {}),
    ...(typeof report.winerror === "number" ? { winerror: report.winerror } : {}),
  };
}

export function reportFromError(error: unknown): ErrorReport | undefined {
  return error && typeof error === "object"
    ? readErrorReport((error as { error_report?: unknown }).error_report) : undefined;
}

export function errorSummary(error: unknown): string {
  const report = reportFromError(error);
  if (report) return report.type + ": " + report.message;
  return error instanceof Error ? error.message : String(error ?? "");
}
