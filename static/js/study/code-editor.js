// code-editor.js: CodeMirror for `code` cards, shared by both study
// hosts. The bundle is large and only these cards need it, so it is
// imported lazily and every failure degrades to the plain textarea the
// caller already rendered: a missing bundle (offline, a cache miss)
// costs syntax highlighting, never the ability to answer.

// The card's language picks a CodeMirror mode; unknown languages get
// no language extension rather than a wrong one.
const LANG_KEYS = ["go", "java", "python", "javascript", "typescript", "rust", "cpp"];

let bundlePromise = null;

function loadBundle(rootPath) {
  if (!bundlePromise) {
    bundlePromise = import(`${rootPath}/static/cm-bundle.js`);
  }
  return bundlePromise;
}

function langExtension(cm, language) {
  const key = (language || "").toLowerCase();
  if (!LANG_KEYS.includes(key)) return [];
  if (key === "typescript") return cm.javascript({typescript: true});
  return cm[key] ? cm[key]() : [];
}

function inputModeExtensions(cm, mode) {
  if (mode === "vim") return [cm.vim()];
  if (mode === "emacs") return [cm.emacs()];
  return [];
}

/**
 * Replace a textarea with a CodeMirror view.
 *
 * The textarea stays in the DOM as the value carrier so the caller's
 * existing `collect()` keeps working untouched; it is hidden rather
 * than removed. Returns a handle, or null when the editor could not
 * mount (the textarea is then still fully usable).
 */
export async function mountCodeEditor(textarea, {rootPath, language, skeleton, inputMode}) {
  let cm;
  try {
    cm = await loadBundle(rootPath);
  } catch (e) {
    return null;
  }
  if (!textarea.isConnected) return null;

  const mount = document.createElement("div");
  mount.className = "cm-mount";
  textarea.parentNode.insertBefore(mount, textarea);

  const modeCompartment = new cm.Compartment();
  const view = new cm.EditorView({
    parent: mount,
    state: cm.EditorState.create({
      doc: textarea.value,
      extensions: [
        modeCompartment.of(inputModeExtensions(cm, inputMode)),
        cm.basicSetup,
        langExtension(cm, language),
        cm.EditorView.lineWrapping,
        cm.EditorView.updateListener.of((u) => {
          if (!u.docChanged) return;
          // The textarea is the value the form submits and the draft
          // autosave reads, so mirror every keystroke into it. The
          // input event is what wakes the caller's autosave.
          textarea.value = u.state.doc.toString();
          textarea.dispatchEvent(new Event("input", {bubbles: true}));
        }),
      ],
    }),
  });

  // Hide the textarea without removing it: still the form's value,
  // no longer focusable or visible.
  textarea.style.position = "absolute";
  textarea.style.opacity = "0";
  textarea.style.pointerEvents = "none";
  textarea.style.height = "1px";
  textarea.style.width = "1px";
  textarea.tabIndex = -1;
  textarea.setAttribute("aria-hidden", "true");

  return {
    view,
    focus: () => view.focus(),
    getValue: () => view.state.doc.toString(),
    setMode: (mode) => {
      view.dispatch({effects: modeCompartment.reconfigure(inputModeExtensions(cm, mode))});
      view.focus();
    },
    reset: () => {
      const fresh = skeleton || "";
      view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: fresh}});
      view.focus();
    },
    destroy: () => view.destroy(),
  };
}

/**
 * The toolbar under a code editor: input-mode picker, copy, and
 * reset-to-skeleton. Same classes the result page used, so
 * components/code-editor.css and the .btn-async chrome still apply.
 * Mode changes are per-card; the profile default lives in settings.
 */
export function codeToolbar(handle, {inputMode, hasSkeleton}) {
  const row = document.createElement("div");
  row.className = "freetext-actions";

  const modeWrap = document.createElement("label");
  modeWrap.className = "code-action code-action-mode";
  modeWrap.title = "Editor input mode (this card only)";
  const select = document.createElement("select");
  select.setAttribute("aria-label", "Editor input mode");
  for (const value of ["vanilla", "vim", "emacs"]) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = value;
    select.appendChild(opt);
  }
  select.value = inputMode || "vanilla";
  select.addEventListener("change", () => handle.setMode(select.value));
  modeWrap.appendChild(select);
  row.appendChild(modeWrap);

  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "code-action btn-async";
  copy.title = "Copy your code to clipboard";
  const copyLabel = document.createElement("span");
  copyLabel.className = "code-action-label";
  copyLabel.textContent = "copy";
  copy.appendChild(copyLabel);
  copy.addEventListener("click", async () => {
    copy.classList.remove("is-success", "is-error");
    try {
      await navigator.clipboard.writeText(handle.getValue());
      copy.classList.add("is-success");
      copyLabel.textContent = "copied";
    } catch (e) {
      copy.classList.add("is-error");
      copyLabel.textContent = "failed";
    }
    window.setTimeout(() => {
      copy.classList.remove("is-success", "is-error");
      copyLabel.textContent = "copy";
    }, 1400);
  });
  row.appendChild(copy);

  if (hasSkeleton) {
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "code-action";
    reset.title = "Restore the skeleton starter code";
    const resetLabel = document.createElement("span");
    resetLabel.className = "code-action-label";
    resetLabel.textContent = "reset";
    reset.appendChild(resetLabel);
    reset.addEventListener("click", () => handle.reset());
    row.appendChild(reset);
  }

  return row;
}
