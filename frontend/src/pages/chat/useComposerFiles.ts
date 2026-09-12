import { useEffect, useRef, useState, type SetStateAction } from "react";
import { patchView, useViewState, viewKey, viewSnapshot } from "../../app/viewState";
import {
  deleteSessionFile,
  searchSessionFiles,
  uploadSessionFiles,
} from "../../api";
import { completionToken, toCandidates, type FileCandidate, type FileTrigger } from "../../commands/fileCompletion";
import type { FileReference } from "../../types";
import type { FileMentionChange, FileMentionEditorHandle } from "./FileMentionEditor";
import type { PendingUpload } from "./contracts";

interface ComposerFilesOptions {
  conversationId?: string;
  sessionId?: string;
  threadId?: string;
  completionDisabled: boolean;
  preserveDraft?: (conversationId?: string) => boolean;
  onTextChanged: (change: FileMentionChange) => void;
}

export function useComposerFiles({
  conversationId,
  sessionId,
  threadId,
  completionDisabled,
  preserveDraft,
  onTextChanged,
}: ComposerFilesOptions) {
  const key = viewKey(sessionId, threadId ?? conversationId);
  const saved = useViewState(key);
  const [input, rawSetInput] = useState("");
  const [references, rawSetReferences] = useState<FileReference[]>([]);
  const [fileTriggerState, setFileTriggerState] = useState<FileTrigger | null>(null);
  const [fileCandidates, setFileCandidates] = useState<FileCandidate[]>([]);
  const [activeFileIndex, setActiveFileIndex] = useState(0);
  const [fileMenuDismissedFor, setFileMenuDismissedFor] = useState<string | null>(null);
  const [pendingUploads, rawSetPendingUploads] = useState<PendingUpload[]>([]);
  const values = useRef({ input, references, pendingUploads });
  const owner = useRef(key);
  owner.current = key;
  const setInput = (action: SetStateAction<string>) => {
    const previous = owner.current === key ? values.current.input : viewSnapshot(key).draft;
    const next = typeof action === "function" ? action(previous) : action;
    if (next === previous) return;
    if (owner.current === key) { values.current.input = next; rawSetInput(next); }
    patchView(key, { draft: next });
  };
  const setReferences = (action: SetStateAction<FileReference[]>) => {
    const previous = owner.current === key ? values.current.references : viewSnapshot(key).references;
    const next = typeof action === "function" ? action(previous) : action;
    if (JSON.stringify(next) === JSON.stringify(previous)) return;
    if (owner.current === key) { values.current.references = next; rawSetReferences(next); }
    patchView(key, { references: next });
  };
  const setPendingUploads = (action: SetStateAction<PendingUpload[]>) => {
    const previous = owner.current === key ? values.current.pendingUploads : viewSnapshot(key).uploads;
    const next = typeof action === "function" ? action(previous) : action;
    if (owner.current === key) { values.current.pendingUploads = next; rawSetPendingUploads(next); }
    patchView(key, { uploads: next.filter((item) => item.status === "done" && item.path).map(({ file: _file, error: _error, ...item }) => item) });
  };
  const fileSearchTimerRef = useRef<number | null>(null);
  const latestFileTriggerRef = useRef<FileTrigger | null>(null);
  const fileMenuDismissedPromptRef = useRef<string | null>(null);
  const discardedUploadUidsRef = useRef(new Set<string>());
  const editorRef = useRef<FileMentionEditorHandle>(null);
  const restoredKey = useRef<string | null>(null);

  useEffect(() => {
    if (restoredKey.current !== key && preserveDraft?.(conversationId)) {
      restoredKey.current = key;
      patchView(key, { draft: values.current.input, references: values.current.references });
      return;
    }
    if (key && !saved.loaded) {
      if (restoredKey.current !== key) {
        restoredKey.current = key;
        values.current = { input: "", references: [], pendingUploads: [] };
        rawSetInput(""); rawSetReferences([]); rawSetPendingUploads([]);
        editorRef.current?.clear();
      }
      return;
    }
    if (restoredKey.current === key && saved.value.draft === values.current.input
      && JSON.stringify(saved.value.references) === JSON.stringify(values.current.references)
      && JSON.stringify(saved.value.uploads) === JSON.stringify(values.current.pendingUploads.filter((item) => item.status === "done"))) return;
    restoredKey.current = key;
    const { draft, references: refs, uploads } = saved.value;
    values.current = { input: draft, references: refs, pendingUploads: uploads };
    rawSetInput(draft); rawSetReferences(refs); rawSetPendingUploads(uploads);
    editorRef.current?.restore(draft, refs);
  }, [key, saved.loaded, saved.value]);

  const fileMenuAvailable = !completionDisabled
    && fileMenuDismissedFor !== input
    && fileTriggerState !== null
    && fileCandidates.length > 0;

  useEffect(() => {
    for (const upload of pendingUploads) {
      if (upload.status === "uploading") discardedUploadUidsRef.current.add(upload.uid);
    }
    setFileTriggerState(null);
    setFileCandidates([]);
    fileMenuDismissedPromptRef.current = null;
    setFileMenuDismissedFor(null);
    // Upload callbacks use the uid set above to delete files that finish after
    // the composer has moved to another conversation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => () => {
    if (fileSearchTimerRef.current !== null) window.clearTimeout(fileSearchTimerRef.current);
  }, []);

  async function searchFiles(trigger: FileTrigger) {
    if (!sessionId) return;
    try {
      const results = await searchSessionFiles(sessionId, trigger.query, 20);
      setFileCandidates(toCandidates(results));
    } catch {
      setFileCandidates([]);
    }
  }

  function handleEditorChange(change: FileMentionChange) {
    const { prompt: value, references: inlineReferences, trigger } = change;
    const dismissedPrompt = fileMenuDismissedPromptRef.current;
    const preserveDismissedMenu = dismissedPrompt === value;
    if (!preserveDismissedMenu && dismissedPrompt !== null) fileMenuDismissedPromptRef.current = null;
    setInput(value);
    setReferences(inlineReferences);
    onTextChanged(change);
    setFileTriggerState(trigger);
    latestFileTriggerRef.current = trigger;
    if (!trigger) {
      if (fileSearchTimerRef.current !== null) {
        window.clearTimeout(fileSearchTimerRef.current);
        fileSearchTimerRef.current = null;
      }
      setFileCandidates([]);
      setFileMenuDismissedFor(null);
      return;
    }
    setFileMenuDismissedFor(preserveDismissedMenu ? value : null);
    setActiveFileIndex(0);
    if (fileSearchTimerRef.current !== null) window.clearTimeout(fileSearchTimerRef.current);
    fileSearchTimerRef.current = window.setTimeout(() => {
      fileSearchTimerRef.current = null;
      if (latestFileTriggerRef.current?.query !== trigger.query || latestFileTriggerRef.current?.start !== trigger.start) return;
      void searchFiles(trigger);
    }, 250);
  }

  function completeFile(index = activeFileIndex) {
    const candidate = fileCandidates[index];
    if (!candidate || !fileTriggerState) return;
    const token = completionToken(candidate.reference.display_path);
    const completedPrompt = `${input.slice(0, fileTriggerState.start)}${token}${input.slice(fileTriggerState.end)}`;
    fileMenuDismissedPromptRef.current = completedPrompt;
    setFileMenuDismissedFor(completedPrompt);
    setFileCandidates([]);
    editorRef.current?.replaceCurrentMention(candidate.reference);
  }

  function handlePickFiles(files: FileList | File[]) {
    if (!sessionId) return;
    const selected = Array.from(files).filter((file) => file.size > 0);
    if (selected.length === 0) return;
    const uploads: PendingUpload[] = selected.map((file) => ({
      uid: crypto.randomUUID(),
      name: file.name,
      isImage: file.type.startsWith("image/"),
      status: "uploading",
      percent: 0,
      file,
    }));
    setPendingUploads((current) => [...current, ...uploads]);
    void uploadSessionFiles(sessionId, selected, (percent) => {
      setPendingUploads((current) => current.map((item) =>
        uploads.some((upload) => upload.uid === item.uid) && item.status === "uploading" ? { ...item, percent } : item));
    }).then((results) => {
      const discardedPaths: string[] = [];
      setPendingUploads((current) => {
        const next = [...current];
        results.forEach((result, index) => {
          const upload = uploads[index];
          if (!upload) return;
          const position = next.findIndex((item) => item.uid === upload.uid);
          if (position === -1 || discardedUploadUidsRef.current.has(upload.uid)) {
            discardedUploadUidsRef.current.delete(upload.uid);
            discardedPaths.push(result.path);
            return;
          }
          next[position] = {
            ...next[position],
            status: "done",
            percent: 100,
            path: result.path,
            displayPath: result.display_path,
          };
        });
        return next;
      });
      for (const path of discardedPaths) void deleteSessionFile(sessionId, "upload", path).catch(() => undefined);
    }).catch((error) => {
      const uploadError = String((error as Error).message ?? error);
      setPendingUploads((current) => current.map((item) =>
        uploads.some((upload) => upload.uid === item.uid) ? { ...item, status: "error", error: uploadError } : item));
    });
  }

  function removePendingUpload(index: number) {
    const upload = pendingUploads[index];
    if (!upload) return;
    if (upload.status === "uploading") discardedUploadUidsRef.current.add(upload.uid);
    if (upload.status === "done" && upload.path && sessionId) {
      void deleteSessionFile(sessionId, "upload", upload.path).catch(() => undefined);
    }
    setPendingUploads((current) => current.filter((_item, itemIndex) => itemIndex !== index));
  }

  function retryUpload(index: number) {
    const upload = pendingUploads[index];
    if (!upload || !sessionId || !upload.file) return;
    setPendingUploads((current) => current.filter((_item, itemIndex) => itemIndex !== index));
    handlePickFiles([upload.file]);
  }

  function collectedReferences(): FileReference[] {
    const uploadedReferences = pendingUploads
      .filter((upload) => upload.status === "done" && upload.path && upload.displayPath)
      .map((upload) => ({
        source: "upload" as const,
        path: upload.path!,
        display_path: upload.displayPath!,
      }));
    return [...references, ...uploadedReferences].filter((reference, index, all) =>
      all.findIndex((candidate) => candidate.source === reference.source && candidate.path === reference.path) === index);
  }

  function clearComposer() {
    if (owner.current === key) editorRef.current?.clear();
    setInput("");
    setReferences([]);
  }

  return {
    input,
    setInput,
    references,
    setReferences,
    pendingUploads,
    setPendingUploads,
    fileCandidates,
    activeFileIndex,
    setActiveFileIndex,
    fileTriggerState,
    fileMenuAvailable,
    setFileMenuDismissedFor,
    editorRef,
    handleEditorChange,
    completeFile,
    handlePickFiles,
    removePendingUpload,
    retryUpload,
    collectedReferences,
    clearComposer,
  };
}
