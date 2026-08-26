// What a page use case is given, and the one result shape `app/http.ts`
// does not carry: a redirect. Neither names Request or Response - the
// cell's route table parses the one and builds the other.
import type { ApiResult } from './http.js';

/** One `multipart/form-data` file part, already read. The three importers
 * are the only routes that take a body a form cannot express. */
export interface Upload {
  filename: string | null;
  bytes: Uint8Array;
}

export interface PageRequest {
  /** Path parameters, already decoded. */
  params: Record<string, string>;
  query: URLSearchParams;
  /** The urlencoded body, empty on a GET. */
  form: URLSearchParams;
  /** `HX-Request: true`, which several routes answer differently. */
  htmx: boolean;
  /** The raw header: `/settings/api/tokens/{id}/delete` branches on any value. */
  hxHeader: string | null;
  userAgent: string | null;
  cookies: Record<string, string>;
  /** The request instant, ISO UTC: the clock is the router's to choose. */
  now: string;
  /** The `file` part on an upload route, null when the caller sent none. */
  upload?: Upload | null;
}

export interface RedirectResult {
  redirect: string;
  /** Land on the Referer instead when it is same-origin. */
  back?: true;
  status?: number;
  headers?: Record<string, string>;
}

export type PageResult = ApiResult | RedirectResult;

export const HTML = 'text/html; charset=utf-8';

export const page = (name: string, context: Record<string, unknown>, status?: number): PageResult => ({ page: name, context, status });
export const redirect = (to: string): PageResult => ({ redirect: to, status: 303 });
export const redirectBack = (to: string): PageResult => ({ redirect: to, back: true, status: 303 });
export const empty = (status = 204): PageResult => ({ empty: true, status });
