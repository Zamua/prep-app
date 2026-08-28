// The `icon(name, class_=, title=)` global over the baked SVG map. The
// class and `aria-hidden` go right after the opening `<svg`, or `role="img"`
// and an `aria-label` when titled. An unknown name renders nothing.
import nunjucks from "nunjucks/browser/nunjucks-slim.js";

const { SafeString } = nunjucks.runtime;

interface IconKwargs {
  class_?: unknown;
  title?: unknown;
  __keywords?: true;
}

export function renderIcon(icons: Record<string, string>, name: string, class_ = "icon", title?: string): string {
  const svg = icons[name];
  if (!svg) return "";
  const openEnd = svg.indexOf(">");
  if (openEnd < 0) return "";
  const attrs = title
    ? ` class="${class_}" role="img" aria-label="${title}"`
    : ` class="${class_}" aria-hidden="true"`;
  return svg.slice(0, openEnd) + attrs + svg.slice(openEnd);
}

export function makeIconGlobal(icons: Record<string, string>) {
  return (name: unknown, kwargs?: IconKwargs): InstanceType<typeof SafeString> => {
    const kw = kwargs && typeof kwargs === "object" ? kwargs : {};
    const class_ = kw.class_ === undefined || kw.class_ === null ? "icon" : String(kw.class_);
    const title = kw.title ? String(kw.title) : undefined;
    return new SafeString(renderIcon(icons, String(name), class_, title));
  };
}
