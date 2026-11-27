<script lang="ts">
  // How far something is: a share of the bar, or a bar that slides while
  // nobody knows how far it is.
  let { done = 0, total = null }: { done?: number; total?: number | null } = $props();
  const share = $derived(total ? Math.min(100, (100 * done) / Math.max(total, 1)) : null);
</script>

<div class="h-1.5 w-full overflow-hidden rounded-full bg-border" role="progressbar" aria-valuenow={share ?? undefined}>
  {#if share === null}
    <div class="sliding h-full w-2/5 rounded-full bg-flow"></div>
  {:else}
    <!-- The gradient spans the whole bar: it shows more of it as it fills. -->
    <div
      class="h-full rounded-full bg-flow transition-[width] duration-300"
      style:width="{share}%"
      style:background-size="{10000 / Math.max(share, 1)}% 100%"
    ></div>
  {/if}
</div>
