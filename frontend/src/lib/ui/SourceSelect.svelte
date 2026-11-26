<script lang="ts">
  import type { Source } from "../api";

  // A download source; an optional one can be none.
  let { sources, value = $bindable(), optional = false }: { sources: Source[]; value: string; optional?: boolean } =
    $props();
</script>

<select bind:value required={!optional}>
  <option value="">{optional ? "None" : "Choose…"}</option>
  {#each sources as s (s.name)}
    <option value={s.name}>{s.label} — {s.note}{s.ready ? "" : " (log in first)"}</option>
  {/each}
</select>
