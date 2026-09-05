import type { FileReference, FileSource, SessionFileInfo } from "../../types";
import { apiUrl } from "../transport/base";
import { ApiError, errorFrom, requestRaw } from "../transport/request";
import { windowOperationControl } from "../transport/operationControl";

/** Upload a batch of files; resolves to the stored file metadata. */
export async function uploadSessionFiles(
  sessionId: string,
  files: File[],
  onProgress?: (percent: number) => void,
): Promise<SessionFileInfo[]> {
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
  const url = `/api/sessions/${encodeURIComponent(sessionId)}/files`;
  const response = await windowOperationControl.requestUsing(
    url,
    { method: "POST", body: form },
    { sessionId },
    (targetUrl, init) => fetchWithProgress(apiUrl(targetUrl), init, onProgress),
  );
  if (!response.ok) {
    throw new ApiError(response.status, await errorFrom(response));
  }
  return response.json() as Promise<SessionFileInfo[]>;
}

/** fetch() with upload progress, using XHR under the hood. */
function fetchWithProgress(
  url: string,
  init: RequestInit,
  onProgress?: (percent: number) => void,
): Promise<Response> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(init.method ?? "POST", url);
    new Headers(init.headers).forEach((value, name) => xhr.setRequestHeader(name, value));
    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    xhr.onload = () => {
      const headers = new Headers();
      for (const line of xhr.getAllResponseHeaders().trim().split(/[\r\n]+/)) {
        const index = line.indexOf(":");
        if (index > 0) headers.append(line.slice(0, index).trim(), line.slice(index + 1).trim());
      }
      const response = new Response(xhr.response, {
        status: xhr.status,
        statusText: xhr.statusText,
        headers,
      });
      resolve(response);
    };
    xhr.onerror = () => reject(new Error("上传请求失败"));
    xhr.send(init.body as XMLHttpRequestBodyInit | null);
  });
}

/** Search project + upload roots for one session. */
export async function searchSessionFiles(
  sessionId: string,
  q: string,
  limit = 20,
): Promise<SessionFileInfo[]> {
  const params = new URLSearchParams({ q, limit: String(limit) });
  const response = await fetch(
    apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/files?${params.toString()}`),
    { cache: "no-store" },
  );
  if (!response.ok) {
    throw new ApiError(response.status, await errorFrom(response));
  }
  return response.json() as Promise<SessionFileInfo[]>;
}

/** Build the local content URL for preview/download. */
export function sessionFileContentUrl(
  sessionId: string,
  source: FileSource,
  path: string,
  download = false,
): string {
  const params = new URLSearchParams({ source, path });
  if (download) params.set("download", "true");
  return apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/files/content?${params.toString()}`);
}

/** Delete one session-uploaded file (project files are never deletable). */
export async function deleteSessionFile(
  sessionId: string,
  source: FileSource,
  path: string,
): Promise<void> {
  const params = new URLSearchParams({ source, path });
  const response = await requestRaw(
    `/api/sessions/${encodeURIComponent(sessionId)}/files?${params.toString()}`,
    { method: "DELETE", cache: "no-store", operation: { sessionId } },
  );
  if (!response.ok) {
    throw new ApiError(response.status, await errorFrom(response));
  }
}

/** Probe whether a referenced file is still available for display. */
export async function fileReferenceAvailable(reference: FileReference, sessionId: string): Promise<boolean> {
  const url = sessionFileContentUrl(sessionId, reference.source, reference.path);
  try {
    const response = await fetch(url, { method: "HEAD", cache: "no-store" });
    return response.ok;
  } catch {
    return false;
  }
}
