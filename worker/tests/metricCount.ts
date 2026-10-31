import { renderMetrics } from '../app/metrics.js';

/** A histogram's observation count for one label set, from the exposition
 * the scrape serves. The registry is module-level, so tests compare deltas. */
export function metricCount(family: string, labels: string): number {
  const prefix = `${family}_count{${labels}} `;
  const line = renderMetrics()
    .split('\n')
    .find((l) => l.startsWith(prefix));
  return line ? Number(line.slice(prefix.length)) : 0;
}
