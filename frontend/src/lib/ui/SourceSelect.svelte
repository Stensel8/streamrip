<script lang="ts">
  import type { Source } from "../api";

  // A download source; an optional one can be none. The sources chosen before
  // this one (`exclude`) are not offered again, and one chosen here goes when
  // it is chosen there.
  let {
    sources,
    value = $bindable(),
    optional = false,
    exclude = [],
  }: { sources: Source[]; value: string; optional?: boolean; exclude?: string[] } = $props();

  const offered = $derived(sources.filter((s) => !exclude.includes(s.name)));
  $effect(() => {
    if (value && exclude.includes(value)) value = "";
  });
</script>

<select bind:value required={!optional}>
  <option value="">{optional ? "None" : "Choose…"}</option>
  {#each offered as s (s.name)}
    <option value={s.name}>{s.label} — {s.note}{s.ready ? "" : " (log in first)"}</option>
  {/each}
</select>
