<script lang="ts">
  // Text to copy with one click, such as a redirect URI.
  let { text }: { text: string } = $props();
  let said = $state("Copy");

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      said = "Copied";
    } catch {
      said = "Select it yourself";
    }
    setTimeout(() => (said = "Copy"), 2000);
  }
</script>

<span class="inline-flex max-w-full items-center gap-2 align-middle"
  ><code>{text}</code><button type="button" class="btn btn-small" onclick={copy}>{said}</button></span
>
