// The 422 bodies FastAPI answers with when a request model fails to
// validate: pydantic v2 error entries under `detail`. Only the shapes
// the JSON surface can actually produce are modelled; an unmodelled
// failure would drift from Python silently, so each one is explicit.

export const PYDANTIC_ERROR_URL = 'https://errors.pydantic.dev/2.13/v/';

export interface PydanticError {
  type: string;
  loc: (string | number)[];
  msg: string;
  input: unknown;
  ctx?: Record<string, unknown>;
  url?: string;
}

export class RequestValidationError extends Error {
  constructor(readonly errors: PydanticError[]) {
    super('request validation failed');
  }
}

export const missing = (loc: (string | number)[], input: unknown): PydanticError => ({
  type: 'missing',
  loc,
  msg: 'Field required',
  input,
});

export const stringType = (loc: (string | number)[], input: unknown): PydanticError => ({
  type: 'string_type',
  loc,
  msg: 'Input should be a valid string',
  input,
});

export const stringTooShort = (loc: (string | number)[], input: unknown, min: number): PydanticError => ({
  type: 'string_too_short',
  loc,
  msg: `String should have at least ${min} character${min === 1 ? '' : 's'}`,
  input,
  ctx: { min_length: min },
});

export const stringTooLong = (loc: (string | number)[], input: unknown, max: number): PydanticError => ({
  type: 'string_too_long',
  loc,
  msg: `String should have at most ${max} character${max === 1 ? '' : 's'}`,
  input,
  ctx: { max_length: max },
});

export const listTooLong = (loc: (string | number)[], input: unknown[], max: number): PydanticError => ({
  type: 'too_long',
  loc,
  msg: `List should have at most ${max} items after validation, not ${input.length}`,
  input,
  ctx: { actual_length: input.length, field_type: 'List', max_length: max },
});

export const modelAttributesType = (loc: (string | number)[], input: unknown): PydanticError => ({
  type: 'model_attributes_type',
  loc,
  msg: 'Input should be a valid dictionary or object to extract fields from',
  input,
});

export const listType = (loc: (string | number)[], input: unknown): PydanticError => ({
  type: 'list_type',
  loc,
  msg: 'Input should be a valid list',
  input,
});

/** The `enum` error `model_validate` raises, carrying pydantic's `url`. */
export function enumError(loc: (string | number)[], input: unknown, allowed: readonly string[]): PydanticError {
  const quoted = allowed.map((v) => `'${v}'`);
  const expected = quoted.length > 1 ? `${quoted.slice(0, -1).join(', ')} or ${quoted[quoted.length - 1]}` : (quoted[0] ?? '');
  return {
    type: 'enum',
    loc,
    msg: `Input should be ${expected}`,
    input,
    ctx: { expected },
    url: `${PYDANTIC_ERROR_URL}enum`,
  };
}

export function intRange(loc: (string | number)[], input: unknown, bound: 'ge' | 'le', value: number): PydanticError {
  const type = bound === 'ge' ? 'greater_than_equal' : 'less_than_equal';
  return {
    type,
    loc,
    msg: `Input should be ${bound === 'ge' ? 'greater than or equal to' : 'less than or equal to'} ${value}`,
    input,
    ctx: { [bound]: value },
    url: `${PYDANTIC_ERROR_URL}${type}`,
  };
}

/** `json_invalid`, whose `ctx.error` is CPython's json message. */
export const jsonInvalid = (message: string, position: number): PydanticError => ({
  type: 'json_invalid',
  loc: ['body', position],
  msg: 'JSON decode error',
  input: {},
  ctx: { error: message },
});

/**
 * CPython's `json.loads` failure, as `{message, position}`. Covers the
 * messages the decoder can raise at the top of a body; an unrecognised
 * failure reports the generic expecting-value message CPython uses.
 */
export function pythonJsonError(text: string): { message: string; position: number } {
  const at = text.search(/\S/);
  if (at < 0) return { message: 'Expecting value', position: 0 };
  // A container that opens and then fails: report the member the decoder
  // was looking for at the first offending character.
  const opener = text[at];
  if (opener === '{') {
    const next = text.slice(at + 1).search(/\S/);
    const pos = next < 0 ? text.length : at + 1 + next;
    if (next < 0 || text[pos] !== '"') return { message: 'Expecting property name enclosed in double quotes', position: pos };
  }
  return { message: 'Expecting value', position: at };
}
