// The Prometheus surface: three histogram families and the text exposition
// encoder for them.
//
// The families keep the reference app's names, label names, bucket
// boundaries and help text, so a query or a dashboard panel written against
// them still resolves. The help text is verbatim down to its mention of a
// framework this runtime does not use: two targets in one scrape job that
// disagree on a family's HELP are an inconsistency Prometheus reports.
//
// The registry is module-level. On this runtime that means per isolate: the
// counters belong to whichever isolate answered the scrape, and they go when
// it is recycled. Nothing here is per cell and nothing survives an eviction.
import { reprFloat } from '../domain/grading/pyrepr.js';

/** One histogram family. */
export interface HistogramSpec {
  readonly name: string;
  readonly help: string;
  /** Label names, in the order `observe` receives their values. */
  readonly labelNames: readonly string[];
  /** Upper bounds, ascending. `+Inf` is implied and always last. */
  readonly buckets: readonly number[];
}

interface Child {
  readonly labels: readonly string[];
  /** One slot per finite bound plus a last slot for everything above them. */
  readonly counts: number[];
  sum: number;
}

export const SPECS = {
  aiGrade: {
    name: 'prep_ai_grade_duration_seconds',
    help: 'Wall-clock duration of one ai_grade call from prompt-build to response-parsed. Labeled by `verdict` (right/wrong/fallback) so we can see fallback rate separately from successful grading latency.',
    labelNames: ['verdict'],
    buckets: [0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 12.0, 15.0, 20.0, 30.0],
  },
  instantGenerate: {
    name: 'prep_instant_generate_duration_seconds',
    help: 'Wall-clock duration of one anonymous instant-generate request, admission to response. Labeled by `outcome` (ok / rate_limited / busy / failed_spent / failed_free / invalid): the spend vs no-spend split is what makes abuse visible separately from upstream trouble.',
    labelNames: ['outcome'],
    buckets: [0.01, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 75.0],
  },
  httpRequest: {
    name: 'prep_http_request_duration_seconds',
    help: 'Request handling time per route. Labels are coarse on purpose: `route` is the route template (e.g. /deck/{name}), not the raw URL, which keeps cardinality bounded.',
    labelNames: ['method', 'route', 'status'],
    buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 7.5, 12.0],
  },
} as const satisfies Record<string, HistogramSpec>;

/** What `Content-Type` the exposition is served as. */
export const METRICS_CONTENT_TYPE = 'text/plain; version=1.0.0; charset=utf-8';

export class Histogram {
  /** Keyed by the label values; iteration order is first-observation order,
   * which is the order the exposition prints its children in. */
  private readonly children = new Map<string, Child>();

  constructor(readonly spec: HistogramSpec) {}

  observe(labels: readonly string[], seconds: number): void {
    if (labels.length !== this.spec.labelNames.length) {
      throw new Error(`${this.spec.name}: ${this.spec.labelNames.length} labels expected, got ${labels.length}`);
    }
    const key = JSON.stringify(labels);
    let child = this.children.get(key);
    if (!child) {
      child = { labels: [...labels], counts: new Array<number>(this.spec.buckets.length + 1).fill(0), sum: 0 };
      this.children.set(key, child);
    }
    child.counts[this.slot(seconds)]! += 1;
    child.sum += seconds;
  }

  /** The bucket a value falls in. A value no bound admits, `NaN` included,
   * lands in `+Inf`, as it does in the reference. */
  private slot(seconds: number): number {
    for (let i = 0; i < this.spec.buckets.length; i++) if (seconds <= this.spec.buckets[i]!) return i;
    return this.spec.buckets.length;
  }

  lines(): string {
    const { name, labelNames, buckets } = this.spec;
    let out = `# HELP ${name} ${escapeHelp(this.spec.help)}\n# TYPE ${name} histogram\n`;
    for (const child of this.children.values()) {
      const pairs = labelNames.map((label, i) => [label, child.labels[i]!] as const);
      let running = 0;
      for (let i = 0; i < buckets.length; i++) {
        running += child.counts[i]!;
        out += sample(`${name}_bucket`, [...pairs, ['le', goString(buckets[i]!)]], running);
      }
      running += child.counts[buckets.length]!;
      out += sample(`${name}_bucket`, [...pairs, ['le', '+Inf']], running);
      out += sample(`${name}_count`, pairs, running);
      out += sample(`${name}_sum`, pairs, child.sum);
    }
    return out;
  }
}

/** The three families together. One per isolate in the runtime; a fresh one
 * per case in a test. */
export class Registry {
  readonly aiGrade = new Histogram(SPECS.aiGrade);
  readonly instantGenerate = new Histogram(SPECS.instantGenerate);
  readonly httpRequest = new Histogram(SPECS.httpRequest);

  /** Registration order, which is the order the exposition prints. */
  private families(): readonly Histogram[] {
    return [this.aiGrade, this.instantGenerate, this.httpRequest];
  }

  render(): string {
    return this.families()
      .map((h) => h.lines())
      .join('');
  }
}

const DEFAULT = new Registry();

export function observeHttpRequest(method: string, route: string, status: number, seconds: number): void {
  DEFAULT.httpRequest.observe([method, route, String(status)], seconds);
}

/** Declared and exposed, with no caller yet: the grading verdict is known
 * inside `aiGrade`, and reporting it from there would weave the metric into
 * the use case. It wants a port the composition root wraps the agent with. */
export function observeAiGrade(verdict: string, seconds: number): void {
  DEFAULT.aiGrade.observe([verdict], seconds);
}

/** Declared and exposed, with no caller yet: the outcome label is finer than
 * the refusal the use case returns, so `generateInstantDeck` would have to
 * carry a metric label in its result type. */
export function observeInstantGenerate(outcome: string, seconds: number): void {
  DEFAULT.instantGenerate.observe([outcome], seconds);
}

export function renderMetrics(): string {
  return DEFAULT.render();
}

// ---- the text exposition format ------------------------------------------

const escapeHelp = (s: string): string => s.replace(/\\/g, '\\\\').replace(/\n/g, '\\n');

const escapeValue = (s: string): string => s.replace(/\\/g, '\\\\').replace(/\n/g, '\\n').replace(/"/g, '\\"');

/** One sample line. Labels print sorted by name, `le` included. */
function sample(name: string, labels: readonly (readonly [string, string])[], value: number): string {
  const rendered = [...labels]
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .map(([k, v]) => `${k}="${escapeValue(v)}"`)
    .join(',');
  return `${name}{${rendered}} ${goString(value)}\n`;
}

/**
 * A float as Go prints it, which is what the exposition format asks for and
 * what `prometheus_client.utils.floatToGoString` produces: `repr(float)`,
 * except that more than seven integer digits switch to an exponent. `repr`
 * is itself where the two languages diverge - JavaScript reaches for an
 * exponent at 1e21 and 1e-7, Python at 1e16 and 1e-5, and neither pads the
 * exponent the way the other does - so the digits come from `reprFloat`
 * rather than from `String(value)`.
 */
export function goString(value: number): string {
  if (value === Infinity) return '+Inf';
  if (value === -Infinity) return '-Inf';
  if (Number.isNaN(value)) return 'NaN';
  const s = reprFloat(value);
  const dot = s.indexOf('.');
  if (value > 0 && dot > 6) {
    const mantissa = `${s[0]}.${s.slice(1, dot)}${s.slice(dot + 1)}`.replace(/[0.]+$/, '');
    return `${mantissa}e+${String(dot - 1).padStart(2, '0')}`;
  }
  return s;
}
