<script lang="ts">
  import type { Snippet } from "svelte";
  import { api, post } from "../lib/api";
  import JobCard from "../lib/JobCard.svelte";
  import { JobSlot } from "../lib/jobs.svelte";
  import { say } from "../lib/notice.svelte";
  import Badge from "../lib/ui/Badge.svelte";
  import Copy from "../lib/ui/Copy.svelte";
  import Field from "../lib/ui/Field.svelte";
  import Menu from "../lib/ui/Menu.svelte";
  import MenuItem from "../lib/ui/MenuItem.svelte";
  import PageHead from "../lib/ui/PageHead.svelte";

  interface Account {
    label: string;
    logged_in: boolean;
    guest: boolean;
    app_missing: boolean;
  }
  interface Data {
    accounts: Record<string, Account>;
    dashboards: Record<string, string>;
    redirect_uris: Record<string, string>;
    soloist: { on: boolean; supported: boolean; key: boolean; paired: boolean; device: string };
    state_path: string;
    tidal_secret: boolean;
  }
  type Plan = { state?: string; plan?: string; formats?: string };

  let data = $state<Data | null>(null);
  let plans = $state<Record<string, Plan>>({});
  // The forms that are open: a login by hand, an app to change.
  let open = $state<Record<string, boolean>>({});
  let given = $state<Record<string, string>>({});

  const load = () => api<Data>("/accounts/api").then((d) => (data = d));
  // What each login is (Free or Paid) and gives, as the last check found:
  // only the Check button asks the services.
  const loadPlans = () =>
    api<Record<string, Plan>>("/accounts/overview")
      .then((p) => (plans = p))
      .catch(() => {});

  const slot = new JobSlot("login verify", (job) => {
    if (job.status !== "done") return;
    load();
    loadPlans();
  });

  load().then(() => loadPlans());
  slot.resume();
  $effect(() => () => slot.stop());

  async function send(url: string, fields: string[]) {
    try {
      say((await post(url, Object.fromEntries(fields.map((f) => [f, given[f] ?? ""])))).message);
      for (const f of fields) given[f] = "";
      open = {};
      await load();
      loadPlans();
    } catch (e) {
      say((e as Error).message, true);
    }
  }

  async function logout(id: string) {
    try {
      say((await post(`/accounts/${id}/logout`)).message);
      await load();
    } catch (e) {
      say((e as Error).message, true);
    }
  }
</script>

{#snippet card(id: string, body: Snippet, menu?: Snippet, badge?: Snippet)}
  {@const a = data!.accounts[id]}
  {@const plan = plans[id] ?? {}}
  <section class="card flex flex-col gap-4">
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0">
        <div class="flex flex-wrap items-center gap-2">
          <h2 class="font-semibold">{a?.label ?? id}</h2>
          {#if badge}{@render badge()}
          {:else if a.logged_in}<Badge tone="ok" text="Logged in" />
          {:else if a.guest}<Badge text="Guest" />
          {:else if a.app_missing}<Badge tone="warn" text="Needs an app" />
          {:else}<Badge text="Not logged in" />{/if}
        </div>
        {#if plan.state === "failed" || plan.plan || plan.formats}
          <p class="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-muted">
            {#if plan.state === "failed"}<Badge tone="warn" text="The login doesn't work" />{/if}
            {#if plan.plan}<Badge tone={plan.plan === "Paid" ? "ok" : "plain"} text={plan.plan} />{/if}
            {plan.formats ?? ""}
          </p>
        {/if}
      </div>
      {#if menu || a?.logged_in}
        <Menu>
          {#if menu}{@render menu()}{/if}
          {#if a?.logged_in}<MenuItem danger text="Log out" onclick={() => logout(id)} />{/if}
        </Menu>
      {/if}
    </div>
    {@render body()}
  </section>
{/snippet}

{#snippet byHand(id: string, url: string, fields: [string, string, boolean][], help: Snippet)}
  {#if open[id]}
    <form
      class="grid gap-3 rounded-xl border border-border bg-line p-4 sm:grid-cols-2"
      onsubmit={(e) => {
        e.preventDefault();
        send(url, fields.map(([name]) => name));
      }}
    >
      <p class="text-xs text-muted sm:col-span-full">{@render help()}</p>
      {#each fields as [name, label, secret] (name)}
        <Field {label} wide={fields.length === 1}>
          {#if secret}
            <input type="password" bind:value={given[name]} required autocomplete="off" />
          {:else}
            <input bind:value={given[name]} required autocomplete="off" />
          {/if}
        </Field>
      {/each}
      <div class="flex justify-end gap-2 sm:col-span-full">
        <button type="button" class="btn" onclick={() => (open[id] = false)}>Cancel</button>
        <button class="btn btn-primary" type="submit">Log in</button>
      </div>
    </form>
  {/if}
{/snippet}

<PageHead title="Accounts">
  {#snippet actions()}
    <button type="button" class="btn" disabled={slot.busy} onclick={() => slot.start("/accounts/verify")}
      >Check all logins</button
    >
  {/snippet}
  Every login lives here, and is kept in <code>{data?.state_path ?? "state.toml"}</code>, next to your settings. The
  command line uses the same ones.
</PageHead>

<JobCard {slot} />

{#if data}
  {@const a = data.accounts}
  <h2 class="mb-3 text-xs font-semibold tracking-wider text-muted uppercase">Download from</h2>
  <div class="mb-10 grid gap-6 md:grid-cols-2">
    {#snippet qobuz()}
      <p class="text-sm text-muted">A subscription, or the albums you bought.</p>
      <div class="flex flex-wrap items-center gap-3">
        <button type="button" class="btn btn-primary" disabled={slot.busy} onclick={() => slot.start("/accounts/qobuz/oauth")}
          >{a.qobuz.logged_in ? "Log in again" : "Log in"}</button
        >
        <p class="text-xs text-muted">You log in on Qobuz's own page; streamrip never sees your password.</p>
      </div>
      {#snippet help()}
        Log in at <a class="link" href="https://play.qobuz.com/login" target="_blank" rel="noopener noreferrer"
          >play.qobuz.com</a
        > with DevTools → Network open, find the <strong>user/login</strong> request and copy
        <strong>user.id</strong> and <strong>user_auth_token</strong> from its response.
      {/snippet}
      {@render byHand("qobuz", "/accounts/qobuz/login", [["user_id", "User id", false], ["auth_token", "Token", true]], help)}
    {/snippet}
    {#snippet qobuzMenu()}
      <MenuItem text="Log in with a script" onclick={() => slot.start("/accounts/qobuz/script")} />
      <MenuItem text="Enter the token by hand" onclick={() => (open.qobuz = true)} />
    {/snippet}
    {@render card("qobuz", qobuz, qobuzMenu)}

    {#snippet tidal()}
      <p class="text-sm text-muted">
        A subscription. You get a link to log in with, on any device. With hi-res on (the default) there are two logins,
        one after the other.
      </p>
      <div>
        <button type="button" class="btn btn-primary" disabled={slot.busy} onclick={() => slot.start("/accounts/tidal/login")}
          >{a.tidal.logged_in ? "Log in again" : "Log in"}</button
        >
      </div>
    {/snippet}
    {@render card("tidal", tidal)}

    {#snippet deezer()}
      <p class="text-sm text-muted">A subscription.</p>
      <div class="flex flex-wrap items-center gap-3">
        <button type="button" class="btn btn-primary" disabled={slot.busy} onclick={() => slot.start("/accounts/deezer/script")}
          >{a.deezer.logged_in ? "Log in again" : "Log in with a script"}</button
        >
        <p class="text-xs text-muted">You log in at Deezer yourself; streamrip never sees your password.</p>
      </div>
      {#snippet help()}
        Log in at <a class="link" href="https://www.deezer.com/login" target="_blank" rel="noopener noreferrer"
          >deezer.com</a
        >, open DevTools → Application (or Storage) → Cookies and copy the value of the <strong>arl</strong> cookie.
      {/snippet}
      {@render byHand("deezer", "/accounts/deezer/login", [["arl", "ARL", true]], help)}
    {/snippet}
    {#snippet deezerMenu()}<MenuItem text="Enter the ARL by hand" onclick={() => (open.deezer = true)} />{/snippet}
    {@render card("deezer", deezer, deezerMenu)}

    {#snippet soundcloud()}
      <p class="text-sm text-muted">
        Works as a guest. A login gives better quality and higher limits; if it fails, streamrip goes on as a guest.
      </p>
      {#if !open.soundcloud}
        <div>
          <button type="button" class="btn btn-primary" onclick={() => (open.soundcloud = true)}
            >{a.soundcloud.logged_in ? "Log in again" : "Log in"}</button
          >
        </div>
      {/if}
      {#snippet help()}
        Log in at <a class="link" href="https://soundcloud.com/signin" target="_blank" rel="noopener noreferrer"
          >soundcloud.com</a
        >, open DevTools → Application (or Storage) → Cookies and copy the value of the
        <strong>oauth_token</strong> cookie.
      {/snippet}
      {@render byHand("soundcloud", "/accounts/soundcloud/login", [["oauth_token", "oauth_token", true]], help)}
    {/snippet}
    {@render card("soundcloud", soundcloud)}
  </div>

  <h2 class="mb-3 text-xs font-semibold tracking-wider text-muted uppercase">Playlists and your library</h2>
  <div class="grid gap-6 md:grid-cols-2">
    {#snippet spotify()}
      <p class="text-sm text-muted">
        One login for downloads (the track lists and tags; the audio is set up in the next card) and for your liked
        songs and playlists. It needs an app of your own, and for the app's owner Spotify needs Premium.
      </p>
      {#if a.spotify.app_missing || open.spotifyApp}
        <form
          class="grid gap-3 rounded-xl border border-border bg-line p-4"
          onsubmit={(e) => {
            e.preventDefault();
            send("/accounts/spotify/app", ["client_id"]);
          }}
        >
          <p class="text-xs text-muted">
            Create the app in the <a class="link" href={data!.dashboards.spotify} target="_blank" rel="noopener noreferrer"
              >Spotify developer dashboard</a
            >, tick <strong>Web API</strong> only, and add this redirect URI: <Copy text={data!.redirect_uris.spotify} />
          </p>
          <Field label="Client ID"
            ><input bind:value={given.client_id} required autocomplete="off" placeholder="the Client ID of your app" /></Field
          >
          <div class="flex justify-end gap-2">
            {#if !a.spotify.app_missing}<button type="button" class="btn" onclick={() => (open.spotifyApp = false)}
                >Cancel</button
              >{/if}
            <button class="btn btn-primary" type="submit">Save the app</button>
          </div>
        </form>
      {:else}
        <div>
          <button
            type="button"
            class="btn btn-primary"
            disabled={slot.busy}
            onclick={() => slot.start("/accounts/spotify/login")}>{a.spotify.logged_in ? "Log in again" : "Log in"}</button
          >
        </div>
      {/if}
    {/snippet}
    {#snippet spotifyMenu()}{#if !a.spotify.app_missing}<MenuItem
          text="Use another app"
          onclick={() => (open.spotifyApp = true)}
        />{/if}{/snippet}
    {@render card("spotify", spotify, spotifyMenu)}

    {#snippet soloist()}
      {@const s = data!.soloist}
      <p class="text-sm text-muted">
        Spotify's own audio, at the account's quality. Without it, the audio is a YouTube Music match. It needs Linux,
        Premium and a Soloist API key from the <a
          class="link"
          href="https://developer.spotify.com/dashboard/soloist"
          target="_blank"
          rel="noopener noreferrer">developer dashboard</a
        >, and one pairing.
      </p>
      {#if !s.on}
        <p class="text-sm text-muted">
          It is off: <code>backend</code> in the Spotify settings is not <code>spotify</code>.
          <a class="link" href="/settings">Change it</a>
        </p>
      {:else if !s.supported}
        <p class="text-sm text-muted">
          This computer cannot do it (that needs Linux with PipeWire or PulseAudio, so not a container either), so the
          audio comes from YouTube Music.
        </p>
      {:else}
        <form
          class="flex flex-col gap-3 sm:flex-row sm:items-end"
          onsubmit={(e) => {
            e.preventDefault();
            slot.start("/accounts/soloist/pair", { api_key: given.api_key ?? "" });
          }}
        >
          <Field label="Soloist API key">
            <input
              type="password"
              bind:value={given.api_key}
              required={!s.key}
              autocomplete="off"
              placeholder={s.key ? "saved: paste another to change it" : "the key from the dashboard"}
            />
          </Field>
          <button class="btn btn-primary shrink-0" type="submit" disabled={slot.busy}
            >{s.key && s.paired ? "Pair again" : "Pair"}</button
          >
        </form>
        <p class="text-xs text-muted">
          Pairing waits for you: in the Spotify app on a device on this network, press play, tap <em
            >Connect to a device</em
          > and pick <strong>{s.device}</strong>.
        </p>
      {/if}
    {/snippet}
    {#snippet soloistBadge()}
      {@const s = data!.soloist}
      {#if s.key && s.paired}<Badge tone="ok" text="Paired" />
      {:else if s.key}<Badge tone="warn" text="Not paired" />
      {:else}<Badge text="Not set up" />{/if}
    {/snippet}
    <!-- Not a login of its own: its card has no Log out. -->
    <section class="card flex flex-col gap-4">
      <div class="flex flex-wrap items-center gap-2"><h2 class="font-semibold">Spotify's own audio</h2>{@render soloistBadge()}</div>
      {@render soloist()}
    </section>

    {#snippet tidalLibrary()}
      <p class="text-sm text-muted">
        Reading and changing your Tidal playlists and favourites goes through Tidal's official API, with a developer app
        of your own. Downloading does not need it.
      </p>
      {#if a["tidal-library"].app_missing || open.tidalApp}
        <form
          class="grid gap-3 rounded-xl border border-border bg-line p-4 sm:grid-cols-2"
          onsubmit={(e) => {
            e.preventDefault();
            send("/accounts/tidal-library/app", ["client_id", "client_secret"]);
          }}
        >
          <ol class="list-decimal space-y-1 pl-4 text-xs text-muted sm:col-span-full">
            <li>
              <em>Create New App</em> in the <a class="link" href={data!.dashboards.tidal} target="_blank" rel="noopener noreferrer"
                >TIDAL developer dashboard</a
              >.
            </li>
            <li>Redirect URI: <Copy text={data!.redirect_uris["tidal-library"]} /></li>
            <li>Scopes: collection.read, collection.write, playlists.read, playlists.write and user.read.</li>
            <li>Copy the Client ID and the Client Secret. If Tidal says <strong>11102</strong>, check all three.</li>
          </ol>
          <Field label="Client ID"><input bind:value={given.client_id} required autocomplete="off" /></Field>
          <Field label="Client Secret">
            <input
              type="password"
              bind:value={given.client_secret}
              required={!data!.tidal_secret}
              placeholder={data!.tidal_secret ? "saved" : ""}
              autocomplete="off"
            />
          </Field>
          <div class="flex justify-end gap-2 sm:col-span-full">
            {#if !a["tidal-library"].app_missing}<button type="button" class="btn" onclick={() => (open.tidalApp = false)}
                >Cancel</button
              >{/if}
            <button class="btn btn-primary" type="submit">Save the app</button>
          </div>
        </form>
      {:else}
        <div>
          <button
            type="button"
            class="btn btn-primary"
            disabled={slot.busy}
            onclick={() => slot.start("/accounts/tidal-library/login")}
            >{a["tidal-library"].logged_in ? "Log in again" : "Log in"}</button
          >
        </div>
      {/if}
    {/snippet}
    {#snippet tidalLibraryMenu()}{#if !a["tidal-library"].app_missing}<MenuItem
          text="Use another app"
          onclick={() => (open.tidalApp = true)}
        />{/if}{/snippet}
    {@render card("tidal-library", tidalLibrary, tidalLibraryMenu)}
  </div>
{/if}
