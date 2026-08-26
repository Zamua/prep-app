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

export interface EmptyResult {
  empty: true;
  status?: number;
  headers?: Record<string, string>;
}

export type ApiResult = JsonResult | TextResult | PageResult | EmptyResult;

export const json = (body: unknown, status = 200, headers?: Record<string, string>): JsonResult => ({ json: body, status, headers });

/** FastAPI's `HTTPException` body. */
export const detail = (status: number, message: unknown): JsonResult => ({ json: { detail: message }, status });
