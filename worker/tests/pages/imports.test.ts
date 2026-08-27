// The ceilings in app/decks/importLimits.ts, exercised through the routes
// that enforce them. A cell has no disk: every number here is what stands
// between an upload and the isolate's heap, so each one is asserted as a
// status and a rendered page rather than as a constant read back.
import { zipSync } from 'fflate';
import { describe, expect, it } from 'vitest';
import {
  ARCHIVE_TOO_LARGE,
  EXPORT_TOO_LARGE,
  MAX_APKG_UPLOAD_BYTES,
  MAX_CSV_UPLOAD_BYTES,
  MAX_EXPORT_QUESTIONS,
  MAX_IMPORT_ROWS,
  MAX_PREPDECK_UPLOAD_BYTES,
  rowCapMessage,
  uploadTooLarge,
} from '../../app/decks/importLimits.js';
import { deckExportApkg, deckExportCsv, deckExportPrepdeck } from '../../app/decks/pages.js';
import type { PageRequest } from '../../app/pageResult.js';
import { SqlJsApkg } from '../../runtime/adapters/apkg.js';
import { FflateZip } from '../../runtime/adapters/zip.js';
import { readCapped } from '../../runtime/cells/routes/adapt.js';
import { cell, type Cell } from '../repos/setup.js';
import { harness, type Harness } from './setup.js';

const enc = new TextEncoder();
const BOUNDARY = '----prepCapBoundary';

function multipart(fields: Record<string, string>, upload: { name: string; filename: string; bytes: Uint8Array } | null): Uint8Array {
  const chunks: Uint8Array[] = [];
  for (const [name, value] of Object.entries(fields)) {
    chunks.push(enc.encode(`--${BOUNDARY}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`));
  }
  if (upload) {
    chunks.push(
      enc.encode(
        `--${BOUNDARY}\r\nContent-Disposition: form-data; name="${upload.name}"; filename="${upload.filename}"\r\nContent-Type: application/octet-stream\r\n\r\n`,
      ),
    );
    chunks.push(upload.bytes);
    chunks.push(enc.encode('\r\n'));
  }
  chunks.push(enc.encode(`--${BOUNDARY}--\r\n`));
  const out = new Uint8Array(chunks.reduce((n, c) => n + c.length, 0));
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.length;
  }
  return out;
}

const upload = (h: Harness, path: string, deck: string, filename: string, bytes: Uint8Array): Promise<Response> =>
  h.get(path, {
    method: 'POST',
    body: multipart({ name: deck }, { name: 'file', filename, bytes }),
    headers: { 'content-type': `multipart/form-data; boundary=${BOUNDARY}` },
  });

/** A body of `size` that declares no `Content-Length`, so only the running
 * count can stop it. */
const chunked = (size: number): ReadableStream<Uint8Array> =>
  new ReadableStream<Uint8Array>({
    pull(controller) {
      controller.enqueue(new Uint8Array(size));
      controller.close();
    },
  });

const CSV_HEADER = 'type,topic,prompt,answer,choices,rubric,skeleton,language,answer_regex,explanation\r\n';
const csvRows = (n: number): string => CSV_HEADER + Array.from({ length: n }, (_, i) => `short,,p${i},a${i},,,,,,\r\n`).join('');

/** A zip whose one entry declares far more inflated bytes than the ceiling
 * while the body itself stays tiny. */
const bomb = (name: string): Uint8Array => zipSync({ [name]: new Uint8Array(48 * 1024 * 1024) }, { level: 9 });

describe('readCapped', () => {
  it('decides on the declared length first, whatever the body then holds', async () => {
    // Content-Length is what a client sends ahead of the bytes, so a body
    // that fits is still refused when the declaration does not.
    const request = new Request('https://x.test/', { method: 'POST', body: chunked(1), headers: { 'content-length': '4096' }, duplex: 'half' } as RequestInit);
    expect(await readCapped(request, 10)).toBeNull();
  });

  it('counts a body that declares no length and abandons it at the cap', async () => {
    const request = new Request('https://x.test/', { method: 'POST', body: chunked(64), duplex: 'half' } as RequestInit);
    expect(await readCapped(request, 32)).toBeNull();
  });

  it('answers the whole body when it fits', async () => {
    const request = new Request('https://x.test/', { method: 'POST', body: chunked(32), duplex: 'half' } as RequestInit);
    expect((await readCapped(request, 32))?.byteLength).toBe(32);
  });

  it('answers an empty body rather than null', async () => {
    expect(await readCapped(new Request('https://x.test/', { method: 'POST' }), 32)).toEqual(new Uint8Array(0));
  });
});

describe('the three body ceilings', () => {
  const cases: [string, string, number][] = [
    ['/decks/import-csv', 'deck_import_csv.html', MAX_CSV_UPLOAD_BYTES],
    ['/decks/import-prepdeck', 'deck_import_prepdeck.html', MAX_PREPDECK_UPLOAD_BYTES],
    ['/decks/import-anki', 'deck_import_anki.html', MAX_APKG_UPLOAD_BYTES],
  ];

  for (const [path, template, max] of cases) {
    it(`re-renders ${template} with a 413 for a body past ${max} bytes`, async () => {
      const h = harness();
      const res = await upload(h, path, 'fresh', 'deck.bin', new Uint8Array(max + 1));
      expect(res.status).toBe(413);
      expect(h.rendered()?.template).toBe(template);
      expect(h.rendered()?.context).toMatchObject({ outcome: null, error: uploadTooLarge(max) });
    });
  }

  it('names the limit in megabytes, one decimal only when it needs one', () => {
    expect(uploadTooLarge(MAX_APKG_UPLOAD_BYTES)).toBe('That file is too large. The limit is 8 MB.');
    expect(uploadTooLarge(MAX_PREPDECK_UPLOAD_BYTES)).toBe('That file is too large. The limit is 2 MB.');
    expect(uploadTooLarge(MAX_CSV_UPLOAD_BYTES)).toBe('That file is too large. The limit is 1.5 MB.');
  });

  it('refuses a chunked body past the cap, which declares no length at all', async () => {
    const h = harness();
    const res = await h.get('/decks/import-csv', {
      method: 'POST',
      body: chunked(MAX_CSV_UPLOAD_BYTES + 1),
      headers: { 'content-type': `multipart/form-data; boundary=${BOUNDARY}` },
      duplex: 'half',
    } as RequestInit);
    expect(res.status).toBe(413);
    expect(h.rendered()?.context).toMatchObject({ error: uploadTooLarge(MAX_CSV_UPLOAD_BYTES) });
  });

  it('imports a body just inside the cap', async () => {
    const h = harness();
    const padded = CSV_HEADER + 'short,,p,a,,,,,,\r\n' + '#'.repeat(1000);
    const res = await upload(h, '/decks/import-csv', 'fresh', 'deck.csv', enc.encode(padded));
    expect(res.status).toBe(200);
    expect(h.rendered()?.context['outcome']).toMatchObject({ inserted: 1 });
  });
});

describe('the inflated-entry ceiling', () => {
  it('answers 400 and the archive message for a .prepdeck bomb', async () => {
    const h = harness();
    const res = await upload(h, '/decks/import-prepdeck', 'fresh', 'deck.prepdeck', bomb('cards.csv'));
    expect(res.status).toBe(400);
    expect(h.rendered()?.template).toBe('deck_import_prepdeck.html');
    expect(h.rendered()?.context).toMatchObject({ outcome: null, error: ARCHIVE_TOO_LARGE });
  });

  it('answers 400 and the archive message for an .apkg bomb', async () => {
    const h = harness();
    const res = await upload(h, '/decks/import-anki', 'fresh', 'deck.apkg', bomb('collection.anki21'));
    expect(res.status).toBe(400);
    expect(h.rendered()?.context).toMatchObject({ outcome: null, error: ARCHIVE_TOO_LARGE });
  });

  it('lets an .apkg through whose bulk is media the reader never opens', async () => {
    const h = harness();
    const res = await upload(h, '/decks/import-anki', 'fresh', 'deck.apkg', bomb('media_0'));
    // No collection, so the codec's own refusal rather than the ceiling's.
    expect(res.status).toBe(400);
    expect(h.rendered()?.context['error']).toContain('no collection.anki2');
  });
});

describe('the row ceiling', () => {
  it('inserts up to the cap and says what it stopped at', async () => {
    const h = harness();
    const res = await upload(h, '/decks/import-csv', 'fresh', 'deck.csv', enc.encode(csvRows(MAX_IMPORT_ROWS + 1)));
    expect(res.status).toBe(200);
    expect(h.rendered()?.context['outcome']).toMatchObject({
      inserted: MAX_IMPORT_ROWS,
      errors: [rowCapMessage(MAX_IMPORT_ROWS)],
    });
  });
});

describe('the export ceiling', () => {
  const stocked = (name: string, count: number): Cell => {
    const c = cell();
    const deckId = c.repos.decks.create(name);
    for (let i = 0; i < count; i++) c.repos.questions.add(deckId, { type: 'short', prompt: `p${i}`, answer: 'a' });
    return c;
  };

  const request = (name: string): PageRequest => ({
    params: { name },
    query: new URLSearchParams(),
    form: new URLSearchParams(),
    htmx: false,
    hxHeader: null,
    userAgent: null,
    cookies: {},
    now: '2026-03-14T15:00:00+00:00',
    upload: null,
  });

  it('refuses both binary exports with the hub and a 413', async () => {
    const c = stocked('huge', MAX_EXPORT_QUESTIONS + 1);
    const refusal = { page: 'deck_export.html', context: { deck_name: 'huge', deck_type: 'srs', error: EXPORT_TOO_LARGE }, status: 413 };
    expect(deckExportPrepdeck(c.repos, request('huge'), { zip: new FflateZip() })).toEqual(refusal);
    expect(await deckExportApkg(c.repos, request('huge'), { apkg: new SqlJsApkg(), subject: 'someone' })).toEqual(refusal);
  });

  it('exports a deck at the cap, and the CSV writer has no ceiling at all', async () => {
    const c = stocked('exactly', MAX_EXPORT_QUESTIONS);
    expect(deckExportPrepdeck(c.repos, request('exactly'), { zip: new FflateZip() })).toMatchObject({ status: 200 });
    // One row at a time out of the repository, so nothing bounds it.
    expect(deckExportCsv(c.repos, request('exactly'))).toMatchObject({ status: 200 });
  });
});
