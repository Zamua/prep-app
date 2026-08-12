// dom.js: tiny DOM builders shared by the study components and the
// offline shell. Plain createElement, textContent by default; the
// only markup ever built from strings is the trusted icon path data.

const SVG_NS = "http://www.w3.org/2000/svg";

// Phosphor Light path data (copied from static/icons/*.svg). Inlined
// because the online app's icon() helper is a server-side Jinja
// global and the raw icon files are not in the SW precache. Trusted
// static markup, built via createElementNS, never innerHTML.
const ICON_PATHS = {
  check:
    "M228.24,76.24l-128,128a6,6,0,0,1-8.48,0l-56-56a6,6,0,0,1,8.48-8.48L96,191.51,219.76,67.76a6,6,0,0,1,8.48,8.48Z",
  x:
    "M204.24,195.76a6,6,0,1,1-8.48,8.48L128,136.49,60.24,204.24a6,6,0,0,1-8.48-8.48L119.51,128,51.76,60.24a6,6,0,0,1,8.48-8.48L128,119.51l67.76-67.75a6,6,0,0,1,8.48,8.48L136.49,128Z",
  circle:
    "M128,26A102,102,0,1,0,230,128,102.12,102.12,0,0,0,128,26Zm0,192a90,90,0,1,1,90-90A90.1,90.1,0,0,1,128,218Z",
  dot: "M138,128a10,10,0,1,1-10-10A10,10,0,0,1,138,128Z",
  "arrow-left":
    "M222,128a6,6,0,0,1-6,6H54.49l61.75,61.76a6,6,0,1,1-8.48,8.48l-72-72a6,6,0,0,1,0-8.48l72-72a6,6,0,0,1,8.48,8.48L54.49,122H216A6,6,0,0,1,222,128Z",
  "caret-down":
    "M212.24,100.24l-80,80a6,6,0,0,1-8.48,0l-80-80a6,6,0,0,1,8.48-8.48L128,167.51l75.76-75.75a6,6,0,0,1,8.48,8.48Z",
  "arrow-up-right":
    "M198,64V168a6,6,0,0,1-12,0V78.48L68.24,196.24a6,6,0,0,1-8.48-8.48L177.52,70H88a6,6,0,0,1,0-12H192A6,6,0,0,1,198,64Z",
};

export function icon(name, className = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 256 256");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", className);
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICON_PATHS[name] || "");
  svg.appendChild(path);
  return svg;
}

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// The prelude pattern shared with the online settings pages: eyebrow,
// display headline (with an italic beat), lede.
export function prelude(eyebrowText, headStart, headEm, ledeText) {
  const section = el("section", "prelude");
  section.appendChild(el("p", "eyebrow", eyebrowText));
  const h1 = el("h1", "display", headStart + " ");
  h1.appendChild(el("em", null, headEm));
  h1.appendChild(document.createTextNode("."));
  section.appendChild(h1);
  section.appendChild(el("p", "lede", ledeText));
  return section;
}

export function sectionEyebrow(label, aside) {
  const p = el("p", "section-eyebrow");
  p.appendChild(el("span", null, label));
  p.appendChild(el("span", "rule"));
  if (aside) p.appendChild(el("span", "eyebrow-aside", aside));
  return p;
}

// Format a minute count the way the online result page does:
// min / hr / day / week / month.
export function humanMinutes(m) {
  if (m < 60) return m + " min";
  if (m < 24 * 60) return Math.floor(m / 60) + " hr";
  const days = Math.floor(m / (24 * 60));
  if (days === 1) return "1 day";
  if (days < 7) return days + " days";
  if (days < 30) {
    const weeks = Math.floor(days / 7);
    return weeks + " week" + (weeks > 1 ? "s" : "");
  }
  const months = Math.floor(days / 30);
  return months + " month" + (months > 1 ? "s" : "");
}
