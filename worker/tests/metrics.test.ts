// The exposition against `prometheus_client`. The families a query names
// have to keep their bucket boundaries and their label sets, and a scrape
// has to parse the same way, so the gate is the whole text rather than a
// structural comparison: help lines, sample ordering, float formatting and
// escaping included.
import { describe, expect, it } from 'vitest';
import { goString, METRICS_CONTENT_TYPE, Registry, renderMetrics, type Histogram } from '../app/metrics.js';
import { ENTRY_ROUTES, observe, OTHER_METHOD, routeLabel, serveMetrics, UNMATCHED } from '../runtime/routes/metrics.js';
import worker from '../runtime/worker.js';
import { fakeEnv, req } from './helpers.js';
import { pythonJson } from './pyoracle.js';

const ORACLE = `
import json
from tests.parity.oracles.metrics import SEQUENCE, exposition
print(json.dumps({
    'sequence': [{'family': f, 'labels': labels, 'seconds': s} for f, labels, s in SEQUENCE],
    'exposition': exposition(),
}))
`;

const GO_STRINGS = `
import json
from prometheus_client.utils import floatToGoString
values = [0.0, 1.0, 0.001, 0.005, 0.012, 0.25, 1.012, 12.0, 41.5, 2.0, 30.0, 75.0, 123456.5, 1234567.5, 10000000.0]
print(json.dumps({'values': values, 'rendered': [floatToGoString(v) for v in values]}))
`;

interface Step {
  family: string;
  labels: Record<string, string>;
  seconds: number;
}

const oracle = pythonJson<{ sequence: Step[]; exposition: string }>(ORACLE);

function replay(steps: readonly Step[]): Registry {
  const registry = new Registry();
  const families: Record<string, Histogram> = {
    ai_grade: registry.aiGrade,
    instant: registry.instantGenerate,
    http: registry.httpRequest,
  };
  for (const step of steps) {
    const family = families[step.family];
    if (!family) throw new Error(`the oracle names a family this app does not have: ${step.family}`);
    family.observe(
      family.spec.labelNames.map((name) => {
        const value = step.labels[name];
        if (value === undefined) throw new Error(`${step.family}: no value for ${name}`);
        return value;
      }),
      step.seconds,
    );
  }
  return registry;
}

describe('the exposition against prometheus_client', () => {
  it('renders the reference byte for byte', () => {
    expect(replay(oracle.sequence).render()).toBe(oracle.exposition);
  });

  it('replays a sequence that covers what the format can vary on', () => {
    const text = oracle.exposition;
    // A family with no observation, the escaping, a cumulative bucket, a
    // value past every finite bound, and a non-integral sum.
    expect(text).toContain('# TYPE prep_ai_grade_duration_seconds histogram\n# HELP');
    expect(text).toContain('route="a\\\\b\\"c\\nd"');
    expect(text).toContain('prep_http_request_duration_seconds_sum{method="GET",route="/",status="200"} 1.012');
    expect(text).toContain('prep_http_request_duration_seconds_bucket{le="+Inf",method="GET",route="/deck/{name}",status="500"} 1.0');
  });

  it('formats a float the way Go does', () => {
    const { values, rendered } = pythonJson<{ values: number[]; rendered: string[] }>(GO_STRINGS);
    expect(values.map(goString)).toEqual(rendered);
    // JSON has no literal for it, so the bucket bound comes on its own.
    expect(goString(Infinity)).toBe('+Inf');
  });
});

describe('the route label', () => {
  it('names the template a cell route serves', () => {
    expect(routeLabel('GET', '/deck/algebra')).toBe('/deck/{name}');
    expect(routeLabel('POST', '/deck/algebra/split')).toBe('/deck/{name}/split');
  });

  it('names the entry worker’s own routes', () => {
    expect(routeLabel('GET', '/healthz')).toBe('/healthz');
    expect(routeLabel('GET', '/llms.txt')).toBe('/llms.txt');
    expect(routeLabel('POST', '/api/instant/generate')).toBe('/api/instant/generate');
    expect(routeLabel('GET', '/readyz')).toBe('/readyz');
  });

  it('collapses every asset URL onto one series', () => {
    expect(routeLabel('GET', '/static/js/vce11d0000000/app/main.js')).toBe('/static/js/v{build}/{path:path}');
    expect(routeLabel('GET', '/static/css/vce11d0000000/base.css')).toBe('/static/css/v{build}/{path:path}');
    // The build segment is one segment and is not optional.
    expect(routeLabel('GET', '/static/js//main.js')).toBe(UNMATCHED);
  });

  it('answers <unmatched> for a path no route serves, whatever its shape', () => {
    expect(routeLabel('GET', '/nope')).toBe(UNMATCHED);
    expect(routeLabel('GET', '/deck/algebra/nope')).toBe(UNMATCHED);
    // A route exists, the method does not.
    expect(routeLabel('DELETE', '/healthz')).toBe(UNMATCHED);
  });

  it('never fails a request over a path it cannot decode', () => {
    expect(() => observe('GET', '/deck/%zz', 404, 0.001)).not.toThrow();
  });
});

describe('GET /metrics', () => {
  it('answers the exposition, uncached, and refuses another method', () => {
    const url = new URL('https://parity.example.test/metrics');
    const response = serveMetrics(new Request(url), url)!;
    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toBe(METRICS_CONTENT_TYPE);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(serveMetrics(new Request(url, { method: 'POST' }), url)!.status).toBe(405);
    const other = new URL('https://parity.example.test/healthz');
    expect(serveMetrics(new Request(other), other)).toBeNull();
  });

  it('records a request under the template it matched and the status it answered', async () => {
    const env = fakeEnv();
    await worker.fetch(req('/privacy'), env);
    await worker.fetch(req('/nope-not-a-route'), env);
    const body = await (await worker.fetch(req('/metrics'), env)).text();
    expect(body).toContain('prep_http_request_duration_seconds_count{method="GET",route="/privacy",status="200"} 1.0');
    expect(body).toContain(`prep_http_request_duration_seconds_count{method="GET",route="${UNMATCHED}",status="404"} 1.0`);
  });

  it('does not count the scrape', async () => {
    const env = fakeEnv();
    await worker.fetch(req('/metrics'), env);
    await worker.fetch(req('/metrics'), env);
    expect(renderMetrics()).not.toContain('route="/metrics"');
  });

  it('bounds the method label, which a client picks freely', () => {
    observe('WHAT', '/healthz', 405, 0.001);
    expect(renderMetrics()).toContain(`method="${OTHER_METHOD}"`);
  });
});

describe('the label table', () => {
  it('holds no duplicate, so a path resolves to one template', () => {
    const keys = ENTRY_ROUTES.map(([method, pattern]) => `${method} ${pattern}`);
    expect(keys).toEqual([...new Set(keys)]);
  });
});
