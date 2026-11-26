<script lang="ts">
  import type { Snippet } from "svelte";

  let { label, wide = false, children }: { label: string; wide?: boolean; children: Snippet } = $props();
</script>

<label class="block min-w-0 {wide ? 'sm:col-span-full' : ''}">
  <span class="label">{label}</span>
  {@render children()}
</label>
