<script lang="ts">
  import { fade } from "svelte/transition";
  import { notice } from "../notice.svelte";
</script>

{#if notice.text}
  <div
    transition:fade={{ duration: 150 }}
    role="status"
    class="fixed top-4 left-1/2 z-50 w-max max-w-[calc(100%-2rem)] -translate-x-1/2 rounded-xl border px-4 py-2 text-sm shadow-xl backdrop-blur {notice.error
      ? 'border-error/40 bg-error/15'
      : 'border-ok/40 bg-ok/15'}"
  >
    {notice.text}
  </div>
{/if}
