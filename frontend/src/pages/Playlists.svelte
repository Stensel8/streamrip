<script lang="ts">
  import { api, type Service, type Source } from "../lib/api";
  import JobCard from "../lib/JobCard.svelte";
  import { JobSlot } from "../lib/jobs.svelte";
  import { say } from "../lib/notice.svelte";
  import Field from "../lib/ui/Field.svelte";
  import PageHead from "../lib/ui/PageHead.svelte";
  import SourceSelect from "../lib/ui/SourceSelect.svelte";
  import Tabs from "../lib/ui/Tabs.svelte";

  interface Playlist {
    id: string;
    name: string;
    track_count: number | null;
    readable: boolean;
  }

  let data = $state<{ services: Service[]; sources: Source[] } | null>(null);
  let tab = $state("download");
  const slot = new JobSlot("transfer import export playlist-download playlist-plan");

  api<{ services: Service[]; sources: Source[] }>("/playlists/api").then((d) => (data = d));
  slot.resume();
  $effect(() => () => slot.stop());

  // The playlists of each service, fetched once it is chosen.
  let playlists = $state<Record<string, Playlist[]>>({});
  function listOf(service: string) {
    if (!service || playlists[service]) return;
    playlists[service] = [];
    api<Playlist[]>(`/playlists/${service}/list`).then(
      (list) => (playlists[service] = list.filter((p) => p.readable)),
      (e: Error) => say(e.message, true),
    );
  }

  let download = $state({ service: "", playlist: "", source: "", fallback: "", then: "" });
  let transfer = $state({ source: "", playlist: "", target: "", into: "playlist", name: "", min_score: 0.8 });
  let exporting = $state({ service: "", playlist: "", format: "csv", library: "", folder: "" });
  let importing = $state({ service: "", contains: "tracks", into: "playlist", playlist: "streamrip import", min_score: 0.8 });
  let importFile = $state<FileList | undefined>();

  $effect(() => listOf(download.service));
  $effect(() => listOf(transfer.source));
  $effect(() => listOf(exporting.service));

  function submit(e: SubmitEvent) {
    e.preventDefault();
    const plan = (e.submitter as HTMLButtonElement | null)?.value === "plan";
    switch (tab) {
      case "download": {
        const { service, ...rest } = download;
        return slot.start(`/playlists/${service}/download`, { ...rest, plan });
      }
      case "transfer":
        // A preview only looks: it changes nothing.
        return slot.start("/playlists/transfer", { ...transfer, dry_run: plan });
      case "export": {
        const { service, ...rest } = exporting;
        return slot.start(`/playlists/${service}/export`, rest);
      }
      case "import": {
        const form = new FormData();
        for (const [k, v] of Object.entries(importing)) form.set(k, String(v));
        if (plan) form.set("dry_run", "1");
        if (importFile?.[0]) form.set("file", importFile[0]);
        return slot.start(`/playlists/${importing.service}/import`, form);
      }
    }
  }
</script>

{#snippet services(value: { [k: string]: unknown }, key: string)}
  <select bind:value={value[key]} required>
    <option value="">Choose…</option>
    {#each data!.services as s (s.name)}<option value={s.name}>{s.label}</option>{/each}
  </select>
{/snippet}

{#snippet which(service: string, value: { playlist: string }, everything = false)}
  <select bind:value={value.playlist}>
    <option value="">Liked songs</option>
    {#if everything}<option value="*">Everything: liked songs and every playlist</option>{/if}
    {#each playlists[service] ?? [] as p (p.id)}
      <option value={p.id} disabled={p.track_count === 0}
        >{p.name} ({p.track_count === 0 ? "empty" : (p.track_count ?? "?")})</option
      >
    {/each}
  </select>
{/snippet}

{#snippet choice(value: { [k: string]: unknown }, key: string, options: [string, string][])}
  <div class="inline-flex w-full rounded-lg border border-border bg-line p-0.5">
    {#each options as [v, text] (v)}
      <button
        type="button"
        class="flex-1 rounded-md px-3 py-1.5 text-sm transition {value[key] === v ? 'bg-raised text-text shadow' : 'text-muted'}"
        onclick={() => (value[key] = v)}>{text}</button
      >
    {/each}
  </div>
{/snippet}

{#snippet matching(value: { min_score: number })}
  <Field label="A match needs a score of">
    <input type="number" min="0" max="1" step="0.05" bind:value={value.min_score} />
  </Field>
{/snippet}

{#snippet buttons(action: string)}
  <div class="flex flex-wrap items-end justify-end gap-2 sm:col-span-full">
    <button class="btn" type="submit" value="plan" disabled={slot.busy}>Preview</button>
    <button class="btn btn-primary" type="submit" disabled={slot.busy}>{action}</button>
  </div>
{/snippet}

<PageHead title="Playlists">
  Move liked songs and playlists between Spotify, Tidal and CSV files, or download them from a lossless source. Tracks
  are found by ISRC, or else by title, artist and length; a live version is never taken for the studio one, and nothing
  is added twice.
</PageHead>

<JobCard {slot} />

{#if data}
  {#if !data.services.length}
    <p class="card mb-6 text-sm">
      No playlist service is logged in yet. Log in to Spotify or to the Tidal library on the
      <a class="link" href="/accounts">Accounts</a> page first.
    </p>
  {/if}

  <Tabs
    bind:active={tab}
    tabs={[
      { id: "download", label: "Download" },
      { id: "transfer", label: "Transfer" },
      { id: "export", label: "Export" },
      { id: "import", label: "Import a CSV" },
    ]}
  />

  <section class="card">
    {#if tab === "download"}
      <p class="mb-4 text-sm text-muted">
        Your liked songs or a playlist, each track found on the source you pick: a Spotify playlist in lossless from
        Qobuz, Tidal or Deezer.
      </p>
      <form class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" onsubmit={submit}>
        <Field label="From">{@render services(download, "service")}</Field>
        <Field label="What">{@render which(download.service, download)}</Field>
        <Field label="Download from"><SourceSelect sources={data.sources} bind:value={download.source} /></Field>
        <Field label="Else from"
          ><SourceSelect sources={data.sources} bind:value={download.fallback} optional exclude={[download.source]} /></Field
        >
        <Field label="Then from"
          ><SourceSelect
            sources={data.sources}
            bind:value={download.then}
            optional
            exclude={[download.source, download.fallback]}
          /></Field
        >
        {@render buttons("Download")}
      </form>
    {:else if tab === "transfer"}
      <p class="mb-4 text-sm text-muted">Copy your liked songs or a playlist straight to the other service.</p>
      <form class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" onsubmit={submit}>
        <Field label="From">{@render services(transfer, "source")}</Field>
        <Field label="What">{@render which(transfer.source, transfer)}</Field>
        <Field label="To">{@render services(transfer, "target")}</Field>
        <div>
          <span class="label">Into</span>
          {@render choice(transfer, "into", [["playlist", "A playlist"], ["favorites", "Favourites"]])}
        </div>
        {#if transfer.into === "playlist"}
          <Field label="Playlist name"><input bind:value={transfer.name} placeholder="Same as the source" /></Field>
        {/if}
        {@render matching(transfer)}
        {@render buttons("Transfer")}
      </form>
    {:else if tab === "export"}
      <p class="mb-4 text-sm text-muted">
        Save your liked songs or playlists as CSV, or as M3U8 playlists for a player or server such as Navidrome or
        Jellyfin.
      </p>
      <form class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" onsubmit={submit}>
        <Field label="From">{@render services(exporting, "service")}</Field>
        <Field label="What">{@render which(exporting.service, exporting, true)}</Field>
        <div>
          <span class="label">As</span>
          {@render choice(exporting, "format", [["csv", "CSV"], ["m3u8", "M3U8"]])}
        </div>
        {#if exporting.format === "m3u8"}
          <Field label="Music folder" wide><input bind:value={exporting.library} placeholder="The download folder" /></Field>
        {/if}
        {#if exporting.format === "m3u8" || exporting.playlist === "*"}
          <Field label="Save in" wide>
            <input bind:value={exporting.folder} placeholder="The Playlists folder inside the download folder" />
          </Field>
        {/if}
        <div class="flex justify-end sm:col-span-full">
          <button class="btn btn-primary" type="submit" disabled={slot.busy}>Export</button>
        </div>
      </form>
    {:else}
      <p class="mb-4 text-sm text-muted">
        Add the tracks or albums of a CSV file to a playlist or your favourites. What is already there is skipped.
      </p>
      <form class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" onsubmit={submit}>
        <Field label="File" wide><input type="file" accept=".csv,text/csv" required bind:files={importFile} /></Field>
        <div>
          <span class="label">It lists</span>
          {@render choice(importing, "contains", [["tracks", "Tracks"], ["albums", "Albums"]])}
        </div>
        <Field label="To">{@render services(importing, "service")}</Field>
        {#if importing.contains === "tracks"}
          <div>
            <span class="label">Into</span>
            {@render choice(importing, "into", [["playlist", "A playlist"], ["favorites", "Favourites"]])}
          </div>
          {#if importing.into === "playlist"}
            <Field label="Playlist"><input bind:value={importing.playlist} /></Field>
          {/if}
        {/if}
        {@render matching(importing)}
        {@render buttons("Import")}
      </form>
    {/if}
  </section>
{/if}
