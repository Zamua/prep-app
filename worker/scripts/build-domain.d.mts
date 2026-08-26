export const WORKER: string;
export const REPO: string;
export const TWINS: readonly { entry: string; out: string }[];
export function bundle(entry: string): Promise<string>;
export function buildTwins(repo?: string, only?: string[]): Promise<string[]>;
