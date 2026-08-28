// The refusals a page use case raises. The route adapter turns one into
// the error page or a `{ detail }` body, so no use case builds a Response.

export class AppError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
  }
}

export const notFound = (detail: string): AppError => new AppError(404, detail);
export const badRequest = (detail: string): AppError => new AppError(400, detail);
