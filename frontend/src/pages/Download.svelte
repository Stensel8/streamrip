<script lang="ts">
  import { api, post, type Source } from "../lib/api";
  import JobCard from "../lib/JobCard.svelte";
  import { JobSlot } from "../lib/jobs.svelte";
  import Field from "../lib/ui/Field.svelte";
  import PageHead from "../lib/ui/PageHead.svelte";
  import SourceSelect from "../lib/ui/SourceSelect.svelte";
  import Tabs from "../lib/ui/Tabs.svelte";

  interface Data {
    sources: Source[];
    media_types: string[];
    id_kinds: string[];
    folder: string;
  }
  interface Result {
    source: string;
    media_type: string;
    id: string;
    name: string;
    artist: string | null;
    year: string | null;
    explicit: boolean;
    details: [string, string][];
    image: string | null;
  }

  let data = $state<Data | null>(null);
  let tab = $state("links");
  const slot = new JobSlot("download plan");

  api<Data>("/download/api").then((d) => (data = d));
  slot.resume();
  $effect(() => () => slot.stop());

  // What each form holds.
  let urls = $state("");
  let linksFile = $state<FileList | undefined>();
  let search = $state({ source: "qobuz", media_type: "album", query: "" });
  let byId = $state({ source: "qobuz", media_type: "album", id: "" });
  let csv = $state({ source: "", fallback: "", then: "" });
  let csvFile = $state<FileList | undefined>();
  let lastfm = $state({ url: "", source: "", fallback: "", then: "" });

  // A source that cannot be searched for every kind only offers the ones it can.
  const kinds = $derived(data?.sources.find((s) => s.name === search.source)?.kinds ?? []);
  $effect(() => {
    if (kinds.length && !kinds.includes(search.media_type))
      search.media_type = kinds.includes("album") ? "album" : kinds[0];
  });

  let results = $state<Result[]>([]);
  let chosen = $state<Record<number, boolean>>({});
  let searching = $state(false);
  let searched = $state("");

  async function find(e: SubmitEvent) {
    e.preventDefault();
    searching = true;
    searched = "";
    chosen = {};
    try {
      results = await post<Result[]>("/search", search);
      searched = results.length ? `${results.length} found. Tick what to download.` : "Nothing found.";
    } catch (err) {
      results = [];
      searched = (err as Error).message;
    } finally {
      searching = false;
    }
  }

  const picked = $derived(results.filter((_, i) => chosen[i]));

  // Each form, sent as a download or as a preview of one.
  function send(plan: boolean) {
    switch (tab) {
      case "links": {
        const form = new FormData();
        form.set("urls", urls);
        if (linksFile?.[0]) form.set("file", linksFile[0]);
        if (plan) form.set("plan", "1");
        return slot.start("/download", form);
      }
      case "search": {
        const items = picked.map(({ source, media_type, id }) => ({ source, media_type, id }));
        return slot.start("/download", { items, plan });
      }
      case "id":
        return slot.start("/download/id", { ...byId, plan });
      case "csv": {
        const form = new FormData();
        for (const [k, v] of Object.entries(csv)) form.set(k, v);
        if (csvFile?.[0]) form.set("file", csvFile[0]);
        if (plan) form.set("plan", "1");
        return slot.start("/download/csv", form);
      }
      case "lastfm":
        return slot.start("/download/lastfm", { ...lastfm, plan });
    }
  }

  function submit(e: SubmitEvent) {
    e.preventDefault();
    send((e.submitter as HTMLButtonElement | null)?.value === "plan");
  }
</script>

{#snippet buttons(disabled = false)}
  <div class="flex flex-wrap justify-end gap-2 sm:col-span-full">
    <button class="btn" type="submit" value="plan" disabled={slot.busy || disabled}>Preview</button>
    <button class="btn btn-primary" type="submit" disabled={slot.busy || disabled}>Download</button>
  </div>
{/snippet}

{#snippet fallbacks(chain: { source: string; fallback: string; then: string })}
  <Field label="Download from"><SourceSelect sources={data!.sources} bind:value={chain.source} /></Field>
  <Field label="Else from"
    ><SourceSelect sources={data!.sources} bind:value={chain.fallback} optional exclude={[chain.source]} /></Field
  >
  <Field label="Then from"
    ><SourceSelect
      sources={data!.sources}
      bind:value={chain.then}
      optional
      exclude={[chain.source, chain.fallback]}
    /></Field
  >
{/snippet}

<PageHead title="Download">
  Albums, tracks, playlists, artists and labels from Qobuz, Tidal, Deezer, SoundCloud and Spotify. They go to
  <code>{data?.folder ?? "your Music folder"}</code>. <em>Preview</em> looks everything up and says what would be
  downloaded, without downloading it.
</PageHead>

<JobCard {slot} />

{#if data}
  <Tabs
    bind:active={tab}
    tabs={[
      { id: "links", label: "Links" },
      { id: "search", label: "Search" },
      { id: "id", label: "By id" },
      { id: "csv", label: "CSV list" },
      { id: "lastfm", label: "Last.fm" },
    ]}
  />

  <section class="card">
    {#if tab === "links"}
      <form class="grid gap-4" onsubmit={submit}>
        <Field label="Links, one per line">
          <textarea bind:value={urls} rows="5" placeholder="https://tidal.com/browse/album/234793"></textarea>
        </Field>
        <Field label="Or a text file with links"><input type="file" accept=".txt,text/plain" bind:files={linksFile} /></Field>
        {@render buttons(!urls.trim() && !linksFile?.length)}
      </form>
    {:else if tab === "search"}
      <form class="grid gap-4 sm:grid-cols-[1fr_1fr_2fr_auto] sm:items-end" onsubmit={find}>
        <Field label="Source">
          <select bind:value={search.source}>
            {#each data.sources as s (s.name)}<option value={s.name}>{s.label}</option>{/each}
          </select>
        </Field>
        <Field label="Kind">
          <select bind:value={search.media_type}>
            {#each data.media_types as t (t)}<option value={t} disabled={!kinds.includes(t)}
                >{t[0].toUpperCase() + t.slice(1)}</option
              >{/each}
          </select>
        </Field>
        <Field label="Search for">
          <input bind:value={search.query} required placeholder="linkin park meteora" autocomplete="off" />
        </Field>
        <button class="btn btn-primary" type="submit" disabled={searching}>{searching ? "Searching…" : "Search"}</button>
      </form>
      {#if searched}<p class="mt-4 text-sm text-muted">{searched}</p>{/if}
      {#if results.length}
        <ul class="mt-3 divide-y divide-border">
          {#each results as r, i (i)}
            <li>
              <label class="flex cursor-pointer items-center gap-3 py-3">
                <input type="checkbox" bind:checked={chosen[i]} />
                {#if r.image}<img src={r.image} alt="" loading="lazy" class="size-12 shrink-0 rounded-lg object-cover" />
                {:else}<span class="size-12 shrink-0 rounded-lg bg-line"></span>{/if}
                <span class="min-w-0">
                  <strong class="block truncate text-sm">{r.name}</strong>
                  <span class="block truncate text-xs text-muted"
                    >{[r.artist, r.year, r.explicit ? "explicit" : null].filter(Boolean).join(" · ")}</span
                  >
                  {#if r.details.length}<span class="block truncate text-xs text-muted/80"
                      >{r.details.map(([k, v]) => `${k}: ${v}`).join(" · ")}</span
                    >{/if}
                </span>
              </label>
            </li>
          {/each}
        </ul>
        <form class="mt-2 flex flex-wrap items-center justify-end gap-2" onsubmit={submit}>
          <span class="mr-auto text-sm text-muted">{picked.length} chosen</span>
          <button class="btn" type="submit" value="plan" disabled={slot.busy || !picked.length}>Preview</button>
          <button class="btn btn-primary" type="submit" disabled={slot.busy || !picked.length}>Download</button>
        </form>
      {/if}
    {:else if tab === "id"}
      <p class="mb-4 text-sm text-muted">When you know a source's own id for something.</p>
      <form class="grid gap-4 sm:grid-cols-3" onsubmit={submit}>
        <Field label="Source">
          <select bind:value={byId.source}>
            {#each data.sources as s (s.name)}<option value={s.name}>{s.label}</option>{/each}
          </select>
        </Field>
        <Field label="Kind">
          <select bind:value={byId.media_type}>
            {#each data.id_kinds as t (t)}<option value={t}>{t[0].toUpperCase() + t.slice(1)}</option>{/each}
          </select>
        </Field>
        <Field label="Id"><input bind:value={byId.id} required placeholder="234793" autocomplete="off" /></Field>
        {@render buttons()}
      </form>
    {:else if tab === "csv"}
      <p class="mb-4 text-sm text-muted">
        A list of tracks (from <a class="link" href="/playlists">Playlists</a>, Exportify or a spreadsheet), each searched
        on the source you pick. A track it lacks is looked for on the next source.
      </p>
      <form class="grid gap-4 sm:grid-cols-3" onsubmit={submit}>
        <Field label="File" wide><input type="file" accept=".csv,.tsv,.txt,text/csv" required bind:files={csvFile} /></Field>
        {@render fallbacks(csv)}
        {@render buttons()}
      </form>
    {:else}
      <p class="mb-4 text-sm text-muted">The tracks of a last.fm playlist, each searched on the source you pick.</p>
      <form class="grid gap-4 sm:grid-cols-3" onsubmit={submit}>
        <Field label="Playlist address" wide>
          <input bind:value={lastfm.url} required placeholder="https://www.last.fm/user/…/playlists/…" autocomplete="off" />
        </Field>
        {@render fallbacks(lastfm)}
        {@render buttons()}
      </form>
    {/if}
  </section>
{/if}
