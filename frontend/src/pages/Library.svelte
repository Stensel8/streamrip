<script lang="ts">
  import { api, post } from "../lib/api";
  import JobCard from "../lib/JobCard.svelte";
  import { JobSlot } from "../lib/jobs.svelte";
  import { say } from "../lib/notice.svelte";
  import Menu from "../lib/ui/Menu.svelte";
  import MenuItem from "../lib/ui/MenuItem.svelte";
  import PageHead from "../lib/ui/PageHead.svelte";

  interface Library {
    downloaded: number;
    recent: [string, string, string][];
    failed: number;
    failures: [string, string, string][];
    shown: number;
  }

  let library = $state<Library | null>(null);
  let flat = $state(false);
  const load = () => api<Library>("/library/api").then((l) => (library = l));
  const slot = new JobSlot("repair", load);

  load();
  slot.resume();
  $effect(() => () => slot.stop());

  async function forget(table: "downloads" | "failed", what: string) {
    if (!confirm(`Forget ${what}?`)) return;
    try {
      say((await post(`/library/clear/${table}`)).message);
      await load();
    } catch (e) {
      say((e as Error).message, true);
    }
  }
</script>

{#snippet table(title: string, rows: [string, string, string][], columns: string[])}
  <details class="mt-4 group">
    <summary class="cursor-pointer text-sm text-accent hover:underline">
      Show {rows.length >= (library?.shown ?? 0) ? `the last ${rows.length}` : title.toLowerCase()}
    </summary>
    <div class="mt-3 max-h-[28rem] overflow-auto rounded-xl border border-border">
      <table class="w-full text-left text-xs">
        <thead class="sticky top-0 bg-raised text-muted">
          <tr>{#each columns as c (c)}<th class="px-3 py-2 font-medium">{c}</th>{/each}</tr>
        </thead>
        <tbody>
          {#each rows as row, i (i)}
            <tr class="border-t border-border">
              {#each row as cell, j (j)}<td class="px-3 py-2 {j === 2 ? 'break-all' : 'whitespace-nowrap'}">{cell}</td>{/each}
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </details>
{/snippet}

<PageHead title="Library">
  What streamrip has downloaded, and what failed. A track in the downloaded list is skipped the next time, even if you
  deleted its file: forget it to download it again.
</PageHead>

<JobCard {slot} />

{#if library}
  <div class="grid gap-6 lg:grid-cols-2">
    <section class="card">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h2 class="font-semibold">Downloaded</h2>
          <p class="mt-2 text-3xl font-semibold tabular-nums">{library.downloaded.toLocaleString()}</p>
          <p class="text-sm text-muted">{library.downloaded === 1 ? "track" : "tracks"}</p>
        </div>
        {#if library.downloaded}
          <Menu>
            <MenuItem
              danger
              text="Forget them…"
              onclick={() => forget("downloads", `${library?.downloaded} downloaded tracks (they will be downloaded again)`)}
            />
          </Menu>
        {/if}
      </div>
      {#if library.recent.length}{@render table("Downloaded", library.recent, ["Source", "Id", "File"])}{/if}
    </section>

    <section class="card">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h2 class="font-semibold">Failed</h2>
          <p class="mt-2 text-3xl font-semibold tabular-nums {library.failed ? 'text-error' : ''}">
            {library.failed.toLocaleString()}
          </p>
          <p class="text-sm text-muted">{library.failed === 1 ? "download" : "downloads"}</p>
        </div>
        {#if library.failed}
          <Menu>
            <MenuItem danger text="Forget them…" onclick={() => forget("failed", `${library?.failed} failed downloads`)} />
          </Menu>
        {/if}
      </div>
      {#if library.failed}
        <p class="mt-4 text-sm text-muted">Retrying goes through the album of each failed track, so repaired tracks rejoin it.</p>
        <div class="mt-3 flex flex-wrap items-center justify-between gap-3">
          <label class="flex items-center gap-2 text-sm text-muted">
            <input type="checkbox" bind:checked={flat} /> Put them straight in the download folder
          </label>
          <button type="button" class="btn btn-primary" disabled={slot.busy} onclick={() => slot.start("/library/repair", { flat })}
            >Retry them</button
          >
        </div>
        {@render table("Failed", library.failures, ["Source", "Kind", "Id"])}
      {/if}
    </section>
  </div>
{/if}
