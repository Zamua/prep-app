// dom.js: tiny DOM builders shared by the study components and the
// offline shell. Plain createElement, textContent by default; the
// only markup ever built from strings is the trusted icon path data.

const SVG_NS = "http://www.w3.org/2000/svg";

// Phosphor Light path data (copied from static/icons/*.svg). Inlined
// because the online app's icon() helper renders server-side and the
// raw icon files are not in the SW precache. Trusted static markup,
// built via createElementNS, never innerHTML.
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
  "push-pin":
    "M233.91,82.79,173.22,22.1a14,14,0,0,0-19.81,0L98.93,76.77c-9.52-3.25-34-8.34-59.71,12.41A14,14,0,0,0,38.1,110l49.71,49.71-44.05,44a6,6,0,1,0,8.48,8.48l44.05-44.05L146,217.89a14,14,0,0,0,9.9,4.11q.49,0,1,0a14,14,0,0,0,10.19-5.54c19.72-26.21,17.15-47.23,12.46-59.3l54.37-54.55A14,14,0,0,0,233.91,82.79ZM225.42,94.1h0l-57.27,57.46a6,6,0,0,0-1.11,6.92c9.94,19.88-1.71,40.32-9.54,50.72a2,2,0,0,1-3,.2L46.58,101.51a2,2,0,0,1,.18-3c12.5-10.09,24.5-12.76,33.7-12.76a42.13,42.13,0,0,1,17.25,3.41A6,6,0,0,0,104.64,88L161.9,30.59a2,2,0,0,1,2.83,0l60.69,60.68A2,2,0,0,1,225.42,94.1Z",
  sparkle:
    "M196.89,130.94,144.4,111.6,125.06,59.11a13.92,13.92,0,0,0-26.12,0L79.6,111.6,27.11,130.94a13.92,13.92,0,0,0,0,26.12L79.6,176.4l19.34,52.49a13.92,13.92,0,0,0,26.12,0L144.4,176.4l52.49-19.34a13.92,13.92,0,0,0,0-26.12Zm-4.15,14.86-55.08,20.3a6,6,0,0,0-3.56,3.56l-20.3,55.08a1.92,1.92,0,0,1-3.6,0L89.9,169.66a6,6,0,0,0-3.56-3.56L31.26,145.8a1.92,1.92,0,0,1,0-3.6l55.08-20.3a6,6,0,0,0,3.56-3.56l20.3-55.08a1.92,1.92,0,0,1,3.6,0l20.3,55.08a6,6,0,0,0,3.56,3.56l55.08,20.3a1.92,1.92,0,0,1,0,3.6ZM146,40a6,6,0,0,1,6-6h18V16a6,6,0,0,1,12,0V34h18a6,6,0,0,1,0,12H182V64a6,6,0,0,1-12,0V46H152A6,6,0,0,1,146,40ZM246,88a6,6,0,0,1-6,6H230v10a6,6,0,0,1-12,0V94H208a6,6,0,0,1,0-12h10V72a6,6,0,0,1,12,0V82h10A6,6,0,0,1,246,88Z",
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
// display headline (with an italic beat), lede. `lineBreak` puts the
// italic beat on its own line, the shape the dashboard headline uses.
export function prelude(eyebrowText, headStart, headEm, ledeText, {lineBreak = false} = {}) {
  const section = el("section", "prelude");
  section.appendChild(el("p", "eyebrow", eyebrowText));
  const h1 = el("h1", "display", headStart);
  h1.appendChild(lineBreak ? document.createElement("br") : document.createTextNode(" "));
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
