<script lang="ts">
  import type { Snippet } from "svelte";
  import Icon, { type IconName } from "./Icon.svelte";

  let { current, children }: { current: string; children: Snippet } = $props();

  const pages: { href: string; label: string; icon: IconName }[] = [
    { href: "/", label: "Download", icon: "download" },
    { href: "/playlists", label: "Playlists", icon: "playlist" },
    { href: "/accounts", label: "Accounts", icon: "user" },
    { href: "/library", label: "Library", icon: "library" },
    { href: "/settings", label: "Settings", icon: "sliders" },
  ];
</script>

<header class="border-b border-border bg-surface">
  <div class="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6">
    <a href="/" class="flex items-center gap-2.5 text-lg font-bold">
      <img src="/static/icon.svg" alt="" class="size-7" />
      <span class="wordmark">streamrip</span>
    </a>
    <nav class="hidden gap-1 md:flex" aria-label="Pages">
      {#each pages as page (page.href)}
        <a
          href={page.href}
          aria-current={page.href === current ? "page" : undefined}
          class="rounded-lg px-3 py-2 text-sm transition {page.href === current
            ? 'bg-line text-text'
            : 'text-muted hover:bg-line hover:text-text'}">{page.label}</a
        >
      {/each}
    </nav>
  </div>
</header>

{@render children()}

<!-- On a phone the pages are a row of tabs at the bottom, where the thumb is. -->
<nav
  class="fixed inset-x-0 bottom-0 z-20 grid grid-cols-5 border-t border-border bg-surface/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden"
  aria-label="Pages"
>
  {#each pages as page (page.href)}
    <a
      href={page.href}
      aria-current={page.href === current ? "page" : undefined}
      class="flex flex-col items-center gap-0.5 py-2 text-[11px] {page.href === current ? 'text-accent' : 'text-muted'}"
    >
      <Icon name={page.icon} />{page.label}
    </a>
  {/each}
</nav>
