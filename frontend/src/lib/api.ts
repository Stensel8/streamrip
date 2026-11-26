// Requests to `streamrip web` (streamrip/web/). Asking for JSON makes it answer
// errors as JSON too, with a message for the person.

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function api<T>(url: string, init: RequestInit = {}): Promise<T> {
  const json = init.body !== undefined && !(init.body instanceof FormData);
  const response = await fetch(url, {
    ...init,
    headers: { Accept: "application/json", ...(json ? { "Content-Type": "application/json" } : {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(body.message ?? `The server said ${response.status}.`, response.status);
  return body as T;
}

/** A POST of JSON, or of a form with its files. */
export const post = <T = { message: string }>(url: string, body: unknown = {}) =>
  api<T>(url, { method: "POST", body: body instanceof FormData ? body : JSON.stringify(body) });

// --- what the pages are told ---------------------------------------------------------

export interface Source {
  name: string;
  label: string;
  note: string;
  // What it can be searched for.
  kinds: string[];
  // Logged in, or needs no login.
  ready: boolean;
}

export interface Service {
  name: string;
  label: string;
}

export type Value = boolean | number | string | string[];

export interface Option {
  name: string;
  label: string;
  group: string;
  help: string;
  kind: "bool" | "int" | "list" | "text" | "secret";
  // The values to choose from, with their labels, where the config lists them.
  choices: [Value, string][];
  // Seldom changed: folded away until asked for.
  advanced: boolean;
  value: Value;
}

export interface Section {
  name: string;
  title: string;
  about: string;
  options: Option[];
}

export interface Settings {
  path: string;
  sections: Section[];
}

export type Changes = Record<string, Record<string, Value>>;
