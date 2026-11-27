<script lang="ts">
  import { fade } from "svelte/transition";
  import { notice } from "../notice.svelte";
</script>

{#if notice.text}
  <div
    transition:fade={{ duration: 150 }}
    role="status"
    class="fixed top-4 left-1/2 z-50 w-max max-w-[calc(100%-2rem)] -translate-x-1/2 overflow-hidden rounded-xl border border-border bg-raised/95 py-2 pr-4 pl-5 text-sm shadow-xl backdrop-blur"
  >
    <span class="absolute inset-y-0 left-0 w-1 {notice.error ? 'bg-error' : 'bg-ok'}" aria-hidden="true"></span>
    {notice.text}
  </div>
{/if}
