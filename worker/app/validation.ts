// The 422 body a request that fails validation answers with: one entry per
// failure under `detail`. Only the shapes the JSON surface can actually
// produce are modelled, each one explicit, so a new failure mode is a
// deliberate addition rather than a silently different body.

export interface ValidationDetail {
  /** Machine-readable failure kind; clients branch on this, not on `msg`. */
  type: string;
  /** Path to the offending value, from the body root. */
  loc: (string | number)[];
  msg: string;
  input: unknown;
  ctx?: Record<string, unknown>;
}

export class RequestValidationError extends Error {
  constructor(readonly errors: ValidationDetail[]) {
    super('request validation failed');
  }
}

export const missing = (loc: (string | number)[], input: unknown): ValidationDetail => ({
  type: 'missing',
  loc,
  msg: 'Field required',
  input,
});

export const stringType = (loc: (string | number)[], input: unknown): ValidationDetail => ({
  type: 'string_type',
  loc,
  msg: 'Input should be a valid string',
  input,
});

export const stringTooShort = (loc: (string | number)[], input: unknown, min: number): ValidationDetail => ({
  type: 'string_too_short',
  loc,
  msg: `String should have at least ${min} character${min === 1 ? '' : 's'}`,
  input,
  ctx: { min_length: min },
});

export const stringTooLong = (loc: (string | number)[], input: unknown, max: number): ValidationDetail => ({
  type: 'string_too_long',
  loc,
  msg: `String should have at most ${max} character${max === 1 ? '' : 's'}`,
  input,
  ctx: { max_length: max },
});

export const listTooLong = (loc: (string | number)[], input: unknown[], max: number): ValidationDetail => ({
  type: 'too_long',
  loc,
  msg: `List should have at most ${max} items after validation, not ${input.length}`,
  input,
  ctx: { actual_length: input.length, field_type: 'List', max_length: max },
});

export const modelAttributesType = (loc: (string | number)[], input: unknown): ValidationDetail => ({
  type: 'model_attributes_type',
  loc,
  msg: 'Input should be a valid dictionary or object to extract fields from',
  input,
});

export const listType = (loc: (string | number)[], input: unknown): ValidationDetail => ({
  type: 'list_type',
  loc,
  msg: 'Input should be a valid list',
  input,
});

/** The value is not one of a fixed set; `ctx.expected` spells the set. */
export function enumError(loc: (string | number)[], input: unknown, allowed: readonly string[]): ValidationDetail {
  const quoted = allowed.map((v) => `'${v}'`);
  const expected = quoted.length > 1 ? `${quoted.slice(0, -1).join(', ')} or ${quoted[quoted.length - 1]}` : (quoted[0] ?? '');
  return {
    type: 'enum',
    loc,
    msg: `Input should be ${expected}`,
    input,
    ctx: { expected },
  };
}

export function intRange(loc: (string | number)[], input: unknown, bound: 'ge' | 'le', value: number): ValidationDetail {
  const type = bound === 'ge' ? 'greater_than_equal' : 'less_than_equal';
  return {
    type,
    loc,
    msg: `Input should be ${bound === 'ge' ? 'greater than or equal to' : 'less than or equal to'} ${value}`,
    input,
    ctx: { [bound]: value },
  };
}

/** The body was not JSON; `ctx.error` names what the decoder wanted. */
export const jsonInvalid = (message: string, position: number): ValidationDetail => ({
  type: 'json_invalid',
  loc: ['body', position],
  msg: 'JSON decode error',
  input: {},
  ctx: { error: message },
});

/**
 * Why a body did not decode, as `{message, position}`. Covers the failures
 * reachable at the top of a body; anything else reports the generic
 * expecting-value message.
 */
export function jsonDecodeFailure(text: string): { message: string; position: number } {
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
