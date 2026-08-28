// What a use case answers with. The runtime turns one of these into a
// Response; the app layer never builds one.

export interface JsonResult {
  json: unknown;
  status?: number;
  headers?: Record<string, string>;
}

export interface TextResult {
  text: string;
  status?: number;
  headers?: Record<string, string>;
}

export interface PageResult {
  page: string;
  context: Record<string, unknown>;
  status?: number;
  headers?: Record<string, string>;
}

/** A download: the codecs answer bytes, not text, and a `.apkg` is not
 * valid UTF-8. */
export interface BytesResult {
  bytes: Uint8Array;
  status?: number;
  headers?: Record<string, string>;
}

export interface EmptyResult {
  empty: true;
  status?: number;
  headers?: Record<string, string>;
}

export type ApiResult = JsonResult | TextResult | BytesResult | PageResult | EmptyResult;

export const json = (body: unknown, status = 200, headers?: Record<string, string>): JsonResult => ({ json: body, status, headers });

/** The refusal body every JSON route answers with: `{ detail }`. */
export const detail = (status: number, message: unknown): JsonResult => ({ json: { detail: message }, status });
