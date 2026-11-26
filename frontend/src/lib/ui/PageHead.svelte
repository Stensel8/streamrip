<script lang="ts">
  import type { Snippet } from "svelte";

  let { title, children, actions }: { title: string; children?: Snippet; actions?: Snippet } = $props();

  $effect(() => {
    document.title = `${title} · streamrip`;
  });
</script>

<header class="mb-6 flex flex-wrap items-start justify-between gap-4">
  <div class="max-w-3xl min-w-0">
    <h1 class="text-2xl font-semibold tracking-tight">{title}</h1>
    {#if children}<p class="mt-1 text-sm text-muted">{@render children()}</p>{/if}
  </div>
  {#if actions}<div class="flex shrink-0 gap-2">{@render actions()}</div>{/if}
</header>
