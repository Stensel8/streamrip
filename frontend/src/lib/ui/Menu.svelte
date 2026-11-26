<script lang="ts">
  import type { Snippet } from "svelte";

  // A "⋯" button with the actions that are not the main one.
  let { label = "More", children }: { label?: string; children: Snippet } = $props();
  let open = $state(false);
  let box: HTMLElement;

  $effect(() => {
    if (!open) return;
    const close = (e: Event) => !box.contains(e.target as Node) && (open = false);
    const escape = (e: KeyboardEvent) => e.key === "Escape" && (open = false);
    addEventListener("click", close);
    addEventListener("keydown", escape);
    return () => {
      removeEventListener("click", close);
      removeEventListener("keydown", escape);
    };
  });
</script>

<div class="relative" bind:this={box}>
  <button type="button" class="btn px-2.5" aria-label={label} aria-expanded={open} onclick={() => (open = !open)}>
    <svg class="size-5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"
      ><circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" /></svg
    >
  </button>
  {#if open}
    <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
    <div
      class="absolute right-0 z-30 mt-2 min-w-48 overflow-hidden rounded-xl border border-border bg-raised py-1 shadow-2xl"
      role="menu"
      tabindex="-1"
      onclick={() => (open = false)}
    >
      {@render children()}
    </div>
  {/if}
</div>
