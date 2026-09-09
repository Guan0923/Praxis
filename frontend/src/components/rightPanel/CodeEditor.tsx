import { useEffect, useRef } from "react";
import { basicSetup, EditorView } from "codemirror";
import { Compartment, EditorState, type Extension } from "@codemirror/state";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { tags } from "@lezer/highlight";
import { useAppearanceMode } from "../../app/AppearanceProvider";
import { palettes } from "../../app/theme";
import type { AppearanceMode } from "../../api/settings";

function editorAppearance(mode: AppearanceMode): Extension {
  const p = palettes[mode];
  return [
    EditorView.theme({
      "&": { color: p.text, backgroundColor: p.surface },
      ".cm-gutters": { color: p.muted, backgroundColor: p["surface-subtle"], borderColor: p.border },
      ".cm-activeLine, .cm-activeLineGutter": { backgroundColor: p.hover },
      ".cm-cursor, .cm-dropCursor": { borderLeftColor: p.accent },
      "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection": { backgroundColor: p.selected },
      ".cm-tooltip, .cm-panels": { color: p.text, backgroundColor: p.surface, borderColor: p.border },
      ".cm-searchMatch": { backgroundColor: p["accent-soft"], outline: `1px solid ${p["accent-border"]}` },
    }, { dark: mode === "dark" }),
    syntaxHighlighting(HighlightStyle.define([
      { tag: tags.keyword, color: p.info },
      { tag: [tags.string, tags.regexp], color: p.success },
      { tag: [tags.number, tags.bool, tags.null], color: p.warning },
      { tag: tags.comment, color: p.muted },
      { tag: [tags.typeName, tags.className, tags.function(tags.variableName)], color: p.accent },
      { tag: tags.invalid, color: p.error },
    ])),
  ];
}

interface CodeEditorProps {
  value: string;
  filename: string;
  newline: "\n" | "\r\n" | "\r";
  readOnly?: boolean;
  onChange: (value: string) => void;
}

async function languageFor(filename: string): Promise<Extension[]> {
  const extension = filename.split(".").pop()?.toLowerCase() ?? "";
  if (["js", "jsx", "mjs", "cjs", "ts", "tsx"].includes(extension)) {
    const { javascript } = await import("@codemirror/lang-javascript");
    return [javascript({ typescript: extension === "ts" || extension === "tsx", jsx: extension === "jsx" || extension === "tsx" })];
  }
  if (extension === "py") {
    const { python } = await import("@codemirror/lang-python");
    return [python()];
  }
  if (extension === "json" || extension === "jsonc") {
    const { json } = await import("@codemirror/lang-json");
    return [json()];
  }
  if (["html", "htm", "vue"].includes(extension)) {
    const { html } = await import("@codemirror/lang-html");
    return [html()];
  }
  if (["css", "scss", "less"].includes(extension)) {
    const { css } = await import("@codemirror/lang-css");
    return [css()];
  }
  if (["md", "markdown"].includes(extension)) {
    const { markdown } = await import("@codemirror/lang-markdown");
    return [markdown()];
  }
  return [];
}

export default function CodeEditor({ value, filename, newline, readOnly = false, onChange }: CodeEditorProps) {
  const mode = useAppearanceMode();
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const appearanceRef = useRef(new Compartment());
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const syncingRef = useRef(false);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    let disposed = false;
    void languageFor(filename).then((language) => {
      if (disposed || !hostRef.current) return;
      const state = EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          appearanceRef.current.of(editorAppearance(modeRef.current)),
          ...language,
          EditorState.lineSeparator.of(newline),
          EditorState.readOnly.of(readOnly),
          EditorView.contentAttributes.of({ "aria-label": "文件编辑器" }),
          EditorView.updateListener.of((update) => {
            if (update.docChanged && !syncingRef.current) onChangeRef.current(update.state.doc.toString());
          }),
          EditorView.theme({
            "&": { height: "100%", fontSize: "13px" },
            ".cm-scroller": { overflow: "auto", fontFamily: "Consolas, monospace" },
            ".cm-content": { minHeight: "100%" },
          }),
        ],
      });
      viewRef.current = new EditorView({ state, parent: hostRef.current });
    });
    return () => {
      disposed = true;
      viewRef.current?.destroy();
      viewRef.current = null;
    };
  }, [filename, newline, readOnly]);

  useEffect(() => {
    viewRef.current?.dispatch({ effects: appearanceRef.current.reconfigure(editorAppearance(mode)) });
  }, [mode]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view || view.state.doc.toString() === value) return;
    syncingRef.current = true;
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: value } });
    syncingRef.current = false;
  }, [value]);

  return <div ref={hostRef} className="file-code-editor" />;
}
