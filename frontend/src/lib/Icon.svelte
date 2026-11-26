<script lang="ts" module>
  // Line icons on a 24 by 24 grid, drawn with the text's colour.
  const icons = {
    download: ["M12 4v11", "m7 10 5 5 5-5", "M5 20h14"],
    playlist: ["M4 6h12", "M4 12h12", "M4 18h7", "M18 19a2 2 0 1 1-2-2h2V8l3-1"],
    user: ["M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M4 21a8 8 0 0 1 16 0"],
    library: ["M4 4h5v16H4z", "M10 4h5v16h-5z", "m16.5 5.5 4 1-3 13.5-4-1z"],
    sliders: ["M4 6h9", "M17 6h3", "M4 12h3", "M11 12h9", "M4 18h11", "M19 18h1", "M15 4v4", "M9 10v4", "M17 16v4"],
    search: ["M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14z", "m20 20-3.5-3.5"],
    chevron: ["m6 9 6 6 6-6"],
    edit: ["M4 20h4L19 9l-4-4L4 16z", "m13 7 4 4"],
  } as const;

  export type IconName = keyof typeof icons;
</script>

<script lang="ts">
  let { name, class: className = "size-5" }: { name: IconName; class?: string } = $props();
</script>

<svg
  class={className}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.8"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
>
  {#each icons[name] as d (d)}<path {d} />{/each}
</svg>
