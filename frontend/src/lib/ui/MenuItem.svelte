<script lang="ts">
  let { onclick, danger = false, text }: { onclick: () => void; danger?: boolean; text: string } = $props();
</script>

<button
  type="button"
  role="menuitem"
  {onclick}
  class="block w-full px-4 py-2 text-left text-sm transition hover:bg-line {danger ? 'text-error' : ''}">{text}</button
>
