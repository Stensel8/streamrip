// A background job of `streamrip web` (web/jobs.py): started by a page, then
// asked how it is getting on until it ends.

import { api, post } from "./api";

export interface Job {
  kind: string;
  title: string;
  status: "running" | "done" | "error";
  phases: string[];
  phase: string;
  text: string;
  done: number;
  total: number | null;
  current: string;
  found: number;
  not_found: number;
  elapsed: number;
  eta: number | null;
  message: string;
  unmatched: { track: string; why: string }[];
  download: boolean;
  report: boolean;
  rows: { desc: string; done: number; total: number | null; eta: number | null }[];
  log: { level: string; text: string }[];
  // A login: the page to log in on, a script to run there, and whether the
  // address a browser elsewhere ends up on can be pasted.
  link: string;
  script: string;
  pasteable: boolean;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// Jobs closed in this tab: a reload does not bring them back.
const dismissed = new Set<string>();
try {
  for (const id of JSON.parse(sessionStorage.getItem("dismissed") ?? "[]")) dismissed.add(id);
} catch {
  // No storage: a closed job just comes back after a reload.
}

/** The one job a page shows: of the `kinds` it starts. */
export class JobSlot {
  id = $state<string | null>(null);
  job = $state<Job | null>(null);
  error = $state("");
  starting = $state(false);
  busy = $derived(this.starting || this.job?.status === "running");

  constructor(
    private kinds: string,
    private ended?: (job: Job) => void,
  ) {}

  /** Start a job and follow it to the end. */
  async start(url: string, body?: unknown) {
    this.error = "";
    this.job = null;
    this.starting = true;
    try {
      const { id } = await post<{ id: string }>(url, body);
      this.starting = false;
      await this.follow(id);
    } catch (e) {
      this.error = (e as Error).message;
    } finally {
      this.starting = false;
    }
  }

  /** A page opened while one of its jobs runs goes on showing it. */
  async resume() {
    try {
      const { id } = await api<{ id: string | null }>(`/jobs/running?kinds=${encodeURIComponent(this.kinds)}`);
      if (id && !dismissed.has(id)) await this.follow(id, true); // a login's page was opened already
    } catch {
      // Not knowing is fine: the page starts empty.
    }
  }

  private async follow(id: string, opened = false) {
    this.id = id;
    for (let failures = 0; this.id === id; ) {
      let job: Job;
      try {
        job = await api<Job>(`/jobs/${id}`);
        failures = 0;
      } catch (e) {
        // One failed poll is no reason to give up; a job the server forgot is gone.
        if ((e as { status?: number }).status === 404 || ++failures > 5) throw e;
        await sleep(2000);
        continue;
      }
      if (this.id !== id) return; // closed, or the page left
      this.job = job;
      // A login without a script opens its page; one with a script waits until
      // its steps were read.
      if (job.kind === "login" && job.link && !job.script && !opened) {
        opened = true;
        window.open(job.link, "_blank", "noopener");
      }
      if (job.status !== "running") {
        if (job.download) location.href = `/jobs/${id}/download`; // saves the file
        this.ended?.(job);
        return;
      }
      await sleep(job.kind === "login" ? 1000 : 500);
    }
  }

  /** Give up waiting for a login (it holds a port, or a script's challenge). */
  cancel() {
    if (this.id) post(`/jobs/${this.id}/cancel`).catch(() => {});
  }

  /** Close the job's card: it goes on, but is not shown again in this tab. */
  dismiss() {
    if (this.id) {
      if (this.job?.status === "running" && (this.job.pasteable || this.job.script)) this.cancel();
      dismissed.add(this.id);
      try {
        sessionStorage.setItem("dismissed", JSON.stringify([...dismissed].slice(-20)));
      } catch {
        // Same.
      }
    }
    this.stop();
  }

  /** Stop following it (the page is left); the job itself goes on. */
  stop() {
    this.id = null;
    this.job = null;
    this.error = "";
  }
}
