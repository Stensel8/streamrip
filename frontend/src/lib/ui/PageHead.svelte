<script lang="ts">
  import type { Snippet } from "svelte";

  // A page's title, with its own buttons on the same row, and what it is for.
  let { title, children, actions }: { title: string; children?: Snippet; actions?: Snippet } = $props();

  $effect(() => {
    document.title = `${title} · streamrip`;
  });
</script>

<header class="mb-6">
  <div class="flex flex-wrap items-center justify-between gap-3">
    <h1 class="text-2xl font-semibold tracking-tight">{title}</h1>
    {#if actions}<div class="flex shrink-0 gap-2">{@render actions()}</div>{/if}
  </div>
  {#if children}<p class="mt-1 max-w-3xl text-sm text-muted">{@render children()}</p>{/if}
</header>
