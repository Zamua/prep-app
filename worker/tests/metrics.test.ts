// The exposition the scrape serves. The families a query names have to keep
// their bucket boundaries and their label sets, and a scrape has to parse the
// same way, so the gate is the whole text rather than a structural
// comparison: help lines, sample ordering, float formatting and escaping
// included.
import { describe, expect, it } from 'vitest';
import { goString, METRICS_CONTENT_TYPE, Registry, renderMetrics, type Histogram } from '../app/metrics.js';
import { ENTRY_ROUTES, observe, OTHER_METHOD, routeLabel, serveMetrics, UNMATCHED } from '../runtime/routes/metrics.js';
import worker from '../runtime/worker.js';
import { fakeEnv, req } from './helpers.js';

interface Step {
  family: string;
  labels: Record<string, string>;
  seconds: number;
}

// A family with no observation, a value in a middle bucket, one past every
// finite bound, a non-integral sum, and a label needing escaping.
const SEQUENCE: readonly Step[] = [
  { family: 'http', labels: { method: 'GET', route: '/', status: '200' }, seconds: 1.012 },
  { family: 'http', labels: { method: 'GET', route: '/deck/{name}', status: '500' }, seconds: 75 },
  { family: 'http', labels: { method: 'POST', route: 'a\\b"c\nd', status: '200' }, seconds: 0.012 },
  { family: 'instant', labels: { outcome: 'ok' }, seconds: 2.0 },
];

const EXPOSITION = [
  "# HELP prep_ai_grade_duration_seconds Wall-clock duration of one ai_grade call from prompt-build to response-parsed. Labeled by `verdict` (right/wrong/fallback) so we can see fallback rate separately from successful grading latency.",
  "# TYPE prep_ai_grade_duration_seconds histogram",
  "# HELP prep_instant_generate_duration_seconds Wall-clock duration of one anonymous instant-generate request, admission to response. Labeled by `outcome` (ok / rate_limited / busy / failed_spent / failed_free / invalid): the spend vs no-spend split is what makes abuse visible separately from upstream trouble.",
  "# TYPE prep_instant_generate_duration_seconds histogram",
  "prep_instant_generate_duration_seconds_bucket{le=\"0.01\",outcome=\"ok\"} 0.0",
  "prep_instant_generate_duration_seconds_bucket{le=\"0.1\",outcome=\"ok\"} 0.0",
  "prep_instant_generate_duration_seconds_bucket{le=\"0.5\",outcome=\"ok\"} 0.0",
  "prep_instant_generate_duration_seconds_bucket{le=\"1.0\",outcome=\"ok\"} 0.0",
  "prep_instant_generate_duration_seconds_bucket{le=\"2.5\",outcome=\"ok\"} 1.0",
  "prep_instant_generate_duration_seconds_bucket{le=\"5.0\",outcome=\"ok\"} 1.0",
  "prep_instant_generate_duration_seconds_bucket{le=\"10.0\",outcome=\"ok\"} 1.0",
  "prep_instant_generate_duration_seconds_bucket{le=\"20.0\",outcome=\"ok\"} 1.0",
  "prep_instant_generate_duration_seconds_bucket{le=\"30.0\",outcome=\"ok\"} 1.0",
  "prep_instant_generate_duration_seconds_bucket{le=\"45.0\",outcome=\"ok\"} 1.0",
  "prep_instant_generate_duration_seconds_bucket{le=\"60.0\",outcome=\"ok\"} 1.0",
  "prep_instant_generate_duration_seconds_bucket{le=\"75.0\",outcome=\"ok\"} 1.0",
  "prep_instant_generate_duration_seconds_bucket{le=\"+Inf\",outcome=\"ok\"} 1.0",
  "prep_instant_generate_duration_seconds_count{outcome=\"ok\"} 1.0",
  "prep_instant_generate_duration_seconds_sum{outcome=\"ok\"} 2.0",
  "# HELP prep_http_request_duration_seconds Request handling time per route. Labels are coarse on purpose: `route` is the route template (e.g. /deck/{name}), not the raw URL, which keeps cardinality bounded.",
  "# TYPE prep_http_request_duration_seconds histogram",
  "prep_http_request_duration_seconds_bucket{le=\"0.005\",method=\"GET\",route=\"/\",status=\"200\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.01\",method=\"GET\",route=\"/\",status=\"200\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.025\",method=\"GET\",route=\"/\",status=\"200\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.05\",method=\"GET\",route=\"/\",status=\"200\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.1\",method=\"GET\",route=\"/\",status=\"200\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.25\",method=\"GET\",route=\"/\",status=\"200\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.5\",method=\"GET\",route=\"/\",status=\"200\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"1.0\",method=\"GET\",route=\"/\",status=\"200\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"2.5\",method=\"GET\",route=\"/\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"5.0\",method=\"GET\",route=\"/\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"7.5\",method=\"GET\",route=\"/\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"12.0\",method=\"GET\",route=\"/\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"+Inf\",method=\"GET\",route=\"/\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_count{method=\"GET\",route=\"/\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_sum{method=\"GET\",route=\"/\",status=\"200\"} 1.012",
  "prep_http_request_duration_seconds_bucket{le=\"0.005\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.01\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.025\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.05\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.1\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.25\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.5\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"1.0\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"2.5\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"5.0\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"7.5\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"12.0\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"+Inf\",method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 1.0",
  "prep_http_request_duration_seconds_count{method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 1.0",
  "prep_http_request_duration_seconds_sum{method=\"GET\",route=\"/deck/{name}\",status=\"500\"} 75.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.005\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.01\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 0.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.025\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.05\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.1\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.25\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"0.5\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"1.0\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"2.5\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"5.0\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"7.5\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"12.0\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_bucket{le=\"+Inf\",method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_count{method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 1.0",
  "prep_http_request_duration_seconds_sum{method=\"POST\",route=\"a\\\\b\\\"c\\nd\",status=\"200\"} 0.012",
  "",
].join('\n');

function replay(steps: readonly Step[]): Registry {
  const registry = new Registry();
  const families: Record<string, Histogram> = {
    ai_grade: registry.aiGrade,
    instant: registry.instantGenerate,
    http: registry.httpRequest,
  };
  for (const step of steps) {
    const family = families[step.family];
    if (!family) throw new Error(`the sequence names a family this app does not have: ${step.family}`);
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

describe('the exposition', () => {
  it('renders the sequence byte for byte', () => {
    expect(replay(SEQUENCE).render()).toBe(EXPOSITION);
  });

  it('covers what the format can vary on', () => {
    expect(EXPOSITION).toContain('# TYPE prep_ai_grade_duration_seconds histogram\n# HELP');
    expect(EXPOSITION).toContain('route="a\\\\b\\"c\\nd"');
    expect(EXPOSITION).toContain('prep_http_request_duration_seconds_sum{method="GET",route="/",status="200"} 1.012');
    expect(EXPOSITION).toContain('prep_http_request_duration_seconds_bucket{le="+Inf",method="GET",route="/deck/{name}",status="500"} 1.0');
  });

  // The Go float format Prometheus reads: how wide an exponent is written,
  // and where a number stops being written out in full.
  it('formats a float the way Go does', () => {
    const values = [
      0.0, -0.0, 1.0, 0.001, 0.005, 0.012, 0.25, 1.012, 12.0, 41.5, 2.0, 30.0, 75.0,
      123456.5, 1234567.5, 10000000.0, 12345678.0, 123456789.5,
      1e-4, 1e-5, 1e-6, 1e-7, 1e-8,
      1e14, 1e15, 1e16, 1e17, 1e21, 1.5e16,
      -1.0, -1e15, -1e16, -0.001,
    ];
    expect(values.map(goString)).toEqual([
      '0.0', '-0.0', '1.0', '0.001', '0.005', '0.012', '0.25', '1.012', '12.0', '41.5', '2.0', '30.0', '75.0',
      '123456.5', '1.2345675e+06', '1e+07', '1.2345678e+07', '1.234567895e+08',
      '0.0001', '1e-05', '1e-06', '1e-07', '1e-08',
      '1e+14', '1e+15', '1e+16', '1e+17', '1e+21', '1.5e+16',
      '-1.0', '-1000000000000000.0', '-1e+16', '-0.001',
    ]);
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
    const url = new URL('https://prep.example.test/metrics');
    const response = serveMetrics(new Request(url), url)!;
    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toBe(METRICS_CONTENT_TYPE);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(serveMetrics(new Request(url, { method: 'POST' }), url)!.status).toBe(405);
    const other = new URL('https://prep.example.test/healthz');
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
