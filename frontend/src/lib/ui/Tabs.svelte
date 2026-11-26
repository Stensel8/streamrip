<script lang="ts">
  // A row of tabs: what a page does, one at a time. It scrolls on a phone.
  let { tabs, active = $bindable() }: { tabs: { id: string; label: string }[]; active: string } = $props();
</script>

<div class="-mx-4 mb-4 overflow-x-auto px-4 sm:mx-0 sm:px-0" role="tablist">
  <div class="inline-flex gap-1 rounded-xl border border-border bg-surface p-1">
    {#each tabs as tab (tab.id)}
      <button
        type="button"
        role="tab"
        aria-selected={active === tab.id}
        onclick={() => (active = tab.id)}
        class="shrink-0 rounded-lg px-3.5 py-1.5 text-sm whitespace-nowrap transition {active === tab.id
          ? 'bg-raised text-text shadow'
          : 'text-muted hover:text-text'}">{tab.label}</button
      >
    {/each}
  </div>
</div>
