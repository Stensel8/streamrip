<script lang="ts">
  import { route } from "./lib/router.svelte";
  import Shell from "./lib/Shell.svelte";
  import Notice from "./lib/ui/Notice.svelte";
  import Accounts from "./pages/Accounts.svelte";
  import ConfigFile from "./pages/ConfigFile.svelte";
  import Download from "./pages/Download.svelte";
  import Library from "./pages/Library.svelte";
  import Playlists from "./pages/Playlists.svelte";
  import Receive from "./pages/Receive.svelte";
  import Settings from "./pages/Settings.svelte";

  const pages = {
    "/": Download,
    "/playlists": Playlists,
    "/accounts": Accounts,
    "/library": Library,
    "/settings": Settings,
    "/settings/file": ConfigFile,
  };
  const receive = $derived(/^\/accounts\/(qobuz|deezer)\/receive$/.test(route.path));
  const Page = $derived(pages[route.path as keyof typeof pages]);
</script>

<Notice />
{#if receive}
  <Receive />
{:else}
  <Shell current={route.path === "/settings/file" ? "/settings" : route.path}>
    <main class="mx-auto max-w-6xl px-4 pt-6 pb-36 sm:px-6 md:pb-16 lg:pt-10">
      {#if Page}
        {#key route.path}<Page />{/key}
      {:else}
        <p class="card text-sm">No page here. <a class="link" href="/">Back to Download</a></p>
      {/if}
    </main>
  </Shell>
{/if}
