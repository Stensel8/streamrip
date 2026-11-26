// What `streamrip web` answers at /settings/api (web/settings.py).

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

async function call<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { Accept: "application/json", "Content-Type": "application/json" },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.message ?? `The server said ${response.status}.`);
  return body as T;
}

export const getSettings = () => call<Settings>("/settings/api");

export const saveSettings = (changes: Changes) =>
  call<Settings>("/settings/api", { method: "POST", body: JSON.stringify({ changes }) });
