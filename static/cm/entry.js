// Entry for the CodeMirror bundle that study.html loads. Bundling everything
// here (rather than letting esm.sh resolve transitive deps in the browser)
// avoids the "multiple instances of @codemirror/state are loaded, breaking
// instanceof checks" failure mode you hit on 2026-04-27.
//
// Build with `bun run build` from this dir. The output cm-bundle.js is a
// single ESM file with stable internal references.

export { EditorView, basicSetup } from "codemirror";
export { EditorState, Compartment } from "@codemirror/state";
export { keymap } from "@codemirror/view";
export { go } from "@codemirror/lang-go";
export { java } from "@codemirror/lang-java";
export { python } from "@codemirror/lang-python";
export { javascript } from "@codemirror/lang-javascript";
export { rust } from "@codemirror/lang-rust";
export { cpp } from "@codemirror/lang-cpp";
export { vim } from "@replit/codemirror-vim";
