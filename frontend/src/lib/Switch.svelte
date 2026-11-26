<script lang="ts">
  let {
    id,
    label,
    checked,
    onchange,
  }: { id: string; label: string; checked: boolean; onchange: (on: boolean) => void } = $props();
</script>

<button
  {id}
  type="button"
  role="switch"
  aria-label={label}
  aria-checked={checked}
  onclick={() => onchange(!checked)}
  class="relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition {checked ? 'bg-accent' : 'bg-border'}"
>
  <span
    class="inline-block size-5 rounded-full bg-text shadow transition {checked ? 'translate-x-5.5' : 'translate-x-0.5'}"
  ></span>
</button>
