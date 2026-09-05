export type FileSource = "project" | "upload" | "workspace";

export const fileSourceLabels: Record<FileSource, string> = {
  project: "项目文件",
  upload: "会话上传",
  workspace: "会话文件",
};

export interface FileReference {
  source: FileSource;
  /** Canonical workspace: or project: path returned by the backend. */
  path: string;
  /** Full prefixed relative path used for every user-visible label. */
  display_path: string;
}

export interface SessionFileInfo {
  source: FileSource;
  /** Canonical workspace: or project: path returned by the backend. */
  path: string;
  /** Full prefixed relative path used for every user-visible label. */
  display_path: string;
  name: string;
  size: number;
  mime: string;
  mtime: string;
  is_image: boolean;
}

export type ManagedFileSource = "workspace" | "project";
export type FileEntryKind = "file" | "directory" | "link" | "special";

export interface FileTreeRoot {
  source: ManagedFileSource;
  path: string;
  name: string;
  available: boolean;
}

export interface FileTreeEntry {
  source: ManagedFileSource;
  path: string;
  name: string;
  kind: FileEntryKind;
  size: number | null;
  mtime: string;
  mime: string | null;
  is_image: boolean;
  version: string | null;
}

interface FileEditorBase {
  source: ManagedFileSource;
  path: string;
  name: string;
  size: number;
  mime: string;
  mtime: string;
  version: string;
}

export type FileEditorDocument = FileEditorBase & (
  | { kind: "text"; content: string; encoding: string; bom: boolean; newline: "\n" | "\r\n" | "\r" }
  | { kind: "image" | "binary" }
  | { kind: "too_large"; limit: number }
  | { kind: "encoding_required"; encodings: string[] }
);
