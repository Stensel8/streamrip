<script lang="ts">
  import { fly } from "svelte/transition";
  import { post } from "./api";
  import type { JobSlot } from "./jobs.svelte";
  import Bar from "./ui/Bar.svelte";

  // A job of the page: how far it is, the tracks in flight, what was not
  // found, and for a login the page to log in on.
  let { slot }: { slot: JobSlot } = $props();

  const PHASES: Record<string, string> = {
    read: "Read",
    match: "Match",
    check: "Compare",
    add: "Add",
    find: "Look up",
    download: "Download",
    login: "Log in",
    cd: "CD quality",
    hires: "Hi-res",
  };

  const job = $derived(slot.job);
  const running = $derived(job?.status === "running");
  const failed = $derived(job?.status === "error");
  const at = $derived(job ? Math.max(job.phases.indexOf(job.phase), running ? 0 : -1) : 0);
  let address = $state("");
  let copied = $state(false);
  let pasteError = $state("");

  const count = (n: number) => n.toLocaleString();
  const clock = (seconds: number) => {
    const s = Math.round(seconds);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  // Time left, rounded the way people say it.
  const roughly = (seconds: number) =>
    seconds < 60 ? `${Math.max(10, Math.round(seconds / 10) * 10)} s` : `${Math.round(seconds / 60)} min`;

  const facts = $derived.by(() => {
    if (!job) return "";
    const unit = job.phase === "download" ? " tracks" : "";
    const parts = [];
    if (running && job.total === null && job.done) parts.push(`${count(job.done)} so far`);
    if (running && job.total !== null) parts.push(`${count(job.done)} of ${count(job.total)}${unit}`);
    if (running && job.eta !== null) parts.push(`about ${roughly(job.eta)} left`);
    if (job.found || job.not_found) parts.push(`${count(job.found)} found`, `${count(job.not_found)} not found`);
    if (!running) parts.push(`took ${clock(job.elapsed)}`);
    return parts.join(" · ");
  });

  // The browser tab says how far it is, so it can be followed from another tab.
  $effect(() => {
    if (!job) return;
    const base = document.title.replace(/^.*? · (?=.* · streamrip$)/, "");
    const phase = PHASES[job.phase] ?? "Starting";
    const share = job.total ? ` ${Math.floor((100 * job.done) / Math.max(job.total, 1))}%` : "…";
    document.title = `${running ? `${phase}${share}` : failed ? "Failed" : "Done"} · ${base}`;
    return () => (document.title = base);
  });

  async function copyScript() {
    try {
      await navigator.clipboard.writeText(job?.script ?? "");
      copied = true;
    } catch {
      copied = false;
    }
  }

  async function paste(e: SubmitEvent) {
    e.preventDefault();
    pasteError = "";
    try {
      await post(`/jobs/${slot.id}/paste`, { address });
      address = "";
    } catch (err) {
      pasteError = (err as Error).message;
    }
  }
</script>

{#if slot.starting || slot.error || job}
  <section transition:fly={{ y: -8, duration: 150 }} class="card mb-6" aria-live="polite">
    <div class="flex items-start justify-between gap-4">
      <h2 class="min-w-0 font-semibold">
        {job?.title ?? (slot.later ? "Not just now" : slot.error ? "Something went wrong" : "Starting…")}
      </h2>
      <div class="flex shrink-0 items-center gap-3">
        {#if job}<span class="text-xs text-muted tabular-nums">{clock(job.elapsed)}</span>{/if}
        <button
          type="button"
          class="rounded-md p-1 text-muted hover:bg-raised hover:text-text"
          aria-label="Close"
          onclick={() => slot.dismiss()}
        >
          <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"
            ><path d="M6 6l12 12M18 6 6 18" /></svg
          >
        </button>
      </div>
    </div>

    {#if job && job.phases.length > 1}
      <ol class="mt-4 flex flex-wrap items-center gap-2 text-xs">
        {#each job.phases as phase, i (phase)}
          {@const state =
            job.status === "done" || i < at ? "done" : i === at ? (failed ? "failed" : "current") : "todo"}
          {#if i}<li class="h-px w-6 bg-border" aria-hidden="true"></li>{/if}
          <li class="flex items-center gap-1.5 {state === 'todo' ? 'text-muted' : ''}">
            <span
              class="grid size-5 place-items-center rounded-full border text-[10px] font-semibold {state === 'done'
                ? 'border-ok bg-ok text-page'
                : state === 'current'
                  ? 'border-accent text-accent'
                  : state === 'failed'
                    ? 'border-error bg-error text-page'
                    : 'border-border'}">{state === "done" ? "✓" : state === "failed" ? "!" : i + 1}</span
            >
            {PHASES[phase] ?? phase}
          </li>
        {/each}
      </ol>
    {/if}

    <p class="mt-3 text-sm {failed || (slot.error && !slot.later && !job) ? 'text-error' : ''}">
      {job ? (running ? job.text : job.message) : slot.error}
    </p>

    {#if job && running && job.link}
      <div class="mt-4 space-y-3 rounded-xl border border-border bg-line p-4">
        {#if job.script}
          <p class="text-sm"><strong>1.</strong> Copy this script.</p>
          <textarea readonly rows="5" class="font-mono text-xs" aria-label="The script">{job.script}</textarea>
          <button type="button" class="btn btn-small" onclick={copyScript}>{copied ? "Copied" : "Copy the script"}</button>
          <p class="text-sm"><strong>2.</strong> Open the login page and log in.</p>
        {/if}
        <div class="flex flex-wrap gap-2">
          <a class="btn btn-primary" href={job.link} target="_blank" rel="noopener noreferrer">Open the login page</a>
          {#if job.pasteable || job.script}
            <button type="button" class="btn" onclick={() => slot.cancel()}>Cancel</button>
          {/if}
        </div>
        {#if job.script}
          <p class="text-sm">
            <strong>3.</strong> On that page, press F12, paste the script into the console and press Enter. If the
            browser asks you to type "allow pasting", do that.
          </p>
        {/if}
        {#if job.pasteable}
          <details class="text-sm">
            <summary class="cursor-pointer text-muted">Is the browser on another device?</summary>
            <p class="mt-2 text-xs text-muted">
              Log in there. It ends on a page that cannot be reached: paste that page's address here.
            </p>
            <form class="mt-2 flex gap-2" onsubmit={paste}>
              <input bind:value={address} placeholder="The address you end up on" autocomplete="off" />
              <button class="btn" type="submit">Use it</button>
            </form>
            {#if pasteError}<p class="mt-1 text-xs text-error">{pasteError}</p>{/if}
          </details>
        {/if}
      </div>
    {/if}

    {#if slot.starting || running}
      <div class="mt-4"><Bar done={job?.done ?? 0} total={job?.total ?? null} /></div>
    {/if}
    {#if facts}<p class="mt-2 text-xs text-muted">{facts}</p>{/if}
    {#if running && job?.phase === "match" && job.current}
      <p class="mt-1 truncate text-xs text-muted">{job.current}</p>
    {/if}

    {#if running && job?.rows.length}
      <ul class="mt-4 space-y-2.5">
        {#each job.rows as row, i (i)}
          <li class="text-xs">
            <p class="mb-1 truncate">{row.desc}{row.eta === null ? "" : ` · about ${roughly(row.eta)} left`}</p>
            <Bar done={row.done} total={row.total} />
          </li>
        {/each}
      </ul>
    {/if}

    {#if job?.not_found}
      <div class="mt-4">
        <p class="text-sm font-medium">{running ? "Not found so far" : "Not found"} ({count(job.not_found)})</p>
        <ul class="mt-2 max-h-60 space-y-1 overflow-y-auto text-xs">
          {#each job.unmatched as miss, i (i)}
            <li>{miss.track} <span class="text-muted">— {miss.why}</span></li>
          {/each}
        </ul>
        {#if job.not_found > job.unmatched.length}
          <p class="mt-1 text-xs text-muted">… and {count(job.not_found - job.unmatched.length)} more in the CSV</p>
        {/if}
        {#if !running}<a class="btn btn-small mt-2" href="/jobs/{slot.id}/unmatched.csv">Download all (CSV)</a>{/if}
      </div>
    {/if}

    {#if job && (job.download || job.report)}
      <div class="mt-4 flex flex-wrap gap-2">
        {#if job.download}<a class="btn btn-primary" href="/jobs/{slot.id}/download">Download the CSV again</a>{/if}
        {#if job.report}<a class="btn btn-small" href="/jobs/{slot.id}/report.csv"
            >How every track was matched (CSV)</a
          >{/if}
      </div>
    {/if}

    {#if job?.log.length}
      <details class="mt-4" open={failed || ["verify", "plan", "playlist-plan"].includes(job.kind)}>
        <summary class="cursor-pointer text-sm text-muted">Log</summary>
        <ol class="mt-2 max-h-72 space-y-0.5 overflow-y-auto rounded-lg bg-line p-3 font-mono text-xs">
          {#each job.log as line, i (i)}
            <li class={line.level === "error" ? "text-error" : line.level === "warning" ? "text-warn" : ""}>
              {line.text}
            </li>
          {/each}
        </ol>
      </details>
    {/if}
  </section>
{/if}
