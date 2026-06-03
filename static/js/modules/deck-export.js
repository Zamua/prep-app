/* PWA-friendly deck export download.
 *
 * Plain <a href="...export.prepdeck"> works fine in desktop browsers
 * but breaks on iOS PWAs (Safari's WebKit hijacks the entire view
 * with a full-screen "Open in..." preview that has no obvious back
 * button — see the screenshot from 2026-06-03).
 *
 * The fix is the Web Share API with files. We fetch the export as a
 * Blob, build a File, and call navigator.share() — iOS pops the
 * native share sheet (AirDrop / Files / Mail / Messages), which the
 * user dismisses normally and lands back in the PWA. Same pattern
 * Anki Mobile, Pinterest, Twitter, etc. all use for file downloads
 * inside their apps.
 *
 * Fallback chain:
 *   1. navigator.canShare({files: [...]}) === true   → use share sheet
 *   2. otherwise                                     → blob URL + click()
 *   3. share rejected with AbortError                → silent (user dismissed)
 *   4. share rejected with anything else             → fall back to (2)
 *
 * Buttons opt in via:
 *   <button class="export-btn"
 *           data-export-url="/deck/foo/export.prepdeck"
 *           data-export-filename="foo.prepdeck"
 *           data-export-mime="application/zip">
 */

export function attachDeclarative(root = document) {
  for (const btn of root.querySelectorAll(".export-btn")) {
    if (btn.dataset.wired) continue;
    btn.dataset.wired = "1";
    btn.addEventListener("click", (e) => handleClick(e, btn));
  }
}

async function handleClick(event, btn) {
  event.preventDefault();
  const url = btn.dataset.exportUrl;
  const filename = btn.dataset.exportFilename || "deck-export";
  const mime = btn.dataset.exportMime || "application/octet-stream";
  if (!url) return;

  const originalLabel = btn.innerHTML;
  btn.disabled = true;
  btn.classList.add("is-loading");
  // Keep the button's visible width stable while loading; CSS can't
  // know the new label's width ahead of time, so we lock it here.
  const width = btn.getBoundingClientRect().width;
  btn.style.minWidth = `${width}px`;
  btn.textContent = "Preparing…";

  try {
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const file = new File([blob], filename, { type: mime });

    // Prefer the share sheet when the browser supports sharing files.
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      try {
        await navigator.share({ files: [file], title: filename });
        // Successful share — sheet auto-dismissed, user back in the
        // app. Nothing more to do.
        return;
      } catch (err) {
        // AbortError = user cancelled. Silent: don't fall back to
        // download since that'd be surprising ("I tapped cancel, why
        // is it downloading anyway?").
        if (err && err.name === "AbortError") return;
        // Any other error (permission, security policy on some
        // managed devices): fall through to the regular download.
      }
    }

    // Fallback: programmatic anchor click against a blob URL. The
    // browser handles MIME + filename naturally and the download
    // completes without leaving the page.
    triggerBlobDownload(blob, filename);
  } catch (err) {
    console.error("export failed", err);
    // Last resort: navigate the URL so the user gets *something*
    // (even if it's the full-screen preview on iOS PWA — at least
    // they have the file).
    window.location.href = url;
  } finally {
    btn.disabled = false;
    btn.classList.remove("is-loading");
    btn.innerHTML = originalLabel;
    btn.style.minWidth = "";
  }
}

function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  // Hidden so it doesn't flash on the page even briefly.
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Free the blob URL after a tick — long enough for the browser to
  // have started the download.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
