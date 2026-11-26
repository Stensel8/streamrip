<script lang="ts">
  import { fade, fly } from "svelte/transition";
  import { getSettings, saveSettings, type Changes, type Section, type Settings, type Value } from "./lib/api";
  import Icon from "./lib/Icon.svelte";
  import OptionRow from "./lib/OptionRow.svelte";
  import Shell from "./lib/Shell.svelte";

  let sections = $state<Section[]>([]);
  let path = $state("");
  // Each option's value by "section.name": as edited, and as saved.
  let values = $state<Record<string, Value>>({});
  let saved = $state.raw<Record<string, Value>>({});
  let query = $state("");
  // The sections whose advanced options are shown.
  let unfolded = $state<Record<string, boolean>>({});
  let active = $state("");
  let saving = $state(false);
  let notice = $state<{ text: string; error: boolean } | null>(null);
  let failed = $state("");

  const key = (section: string, option: string) => `${section}.${option}`;
  const same = (a: Value, b: Value) => JSON.stringify(a) === JSON.stringify(b);

  function take(settings: Settings) {
    const fresh: Record<string, Value> = {};
    for (const s of settings.sections) for (const o of s.options) fresh[key(s.name, o.name)] = o.value;
    sections = settings.sections;
    path = settings.path;
    saved = fresh;
    values = structuredClone(fresh);
  }

  getSettings().then(take, (e: Error) => (failed = e.message));

  const changed = $derived(Object.keys(values).filter((k) => !same(values[k], saved[k])));
  const words = $derived(query.trim().toLowerCase());
  // What each section shows: what the search finds, without the advanced
  // options while they are folded away (a search finds those too).
  const shown = $derived(
    sections
      .map((s) => {
        const found = s.options.filter(
          (o) => !words || `${o.label} ${o.name} ${o.help}`.toLowerCase().includes(words),
        );
        const visible = found.filter((o) => words || !o.advanced || unfolded[s.name]);
        return { ...s, visible, folded: found.length - visible.length };
      })
      .filter((s) => s.visible.length || s.folded),
  );

  function say(text: string, error = false) {
    notice = { text, error };
    setTimeout(() => notice?.text === text && (notice = null), 3500);
  }

  async function save() {
    const changes: Changes = {};
    for (const k of changed) {
      const [section, name] = k.split(".");
      (changes[section] ??= {})[name] = $state.snapshot(values[k]) as Value;
    }
    const count = changed.length;
    saving = true;
    try {
      take(await saveSettings(changes));
      say(`Saved ${count} change${count === 1 ? "" : "s"}.`);
    } catch (e) {
      say((e as Error).message, true);
    } finally {
      saving = false;
    }
  }

  function undo() {
    for (const k of changed) values[k] = structuredClone(saved[k]);
  }

  // Leaving with changes that are not saved asks first.
  $effect(() => {
    if (!changed.length) return;
    const ask = (e: BeforeUnloadEvent) => e.preventDefault();
    addEventListener("beforeunload", ask);
    return () => removeEventListener("beforeunload", ask);
  });

  // The section in view is the one the menu marks.
  function spy(node: HTMLElement, name: string) {
    const observer = new IntersectionObserver(([entry]) => entry.isIntersecting && (active = name), {
      rootMargin: "-25% 0px -65% 0px",
    });
    observer.observe(node);
    return { destroy: () => observer.disconnect() };
  }
</script>

<Shell current="/settings">
  <div
    class="mx-auto max-w-6xl px-4 pt-6 pb-48 sm:px-6 lg:grid lg:grid-cols-[13rem_minmax(0,1fr)] lg:gap-10 lg:pt-10"
  >
    <nav class="hidden lg:block" aria-label="Sections">
      <ul class="sticky top-6 space-y-0.5">
        {#each shown as s (s.name)}
          <li>
            <a
              href="#{s.name}"
              class="block rounded-lg px-3 py-1.5 text-sm transition {active === s.name
                ? 'bg-raised text-text'
                : 'text-muted hover:bg-raised/60 hover:text-text'}">{s.title}</a
            >
          </li>
        {/each}
      </ul>
    </nav>

    <main class="min-w-0">
      <header class="mb-5">
        <h1 class="text-2xl font-semibold tracking-tight">Settings</h1>
        <p class="mt-1 max-w-2xl text-sm text-muted">
          Every option of your config file. A change counts from the next download; the command line
          reads it at its next run.
        </p>
        <label class="relative mt-5 block">
          <span class="sr-only">Search the settings</span>
          <Icon name="search" class="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted" />
          <input type="search" bind:value={query} placeholder="Search the settings" class="w-full pl-9" />
        </label>
      </header>

      <!-- On a narrow screen the sections are a row of chips that stays in view. -->
      <nav
        class="sticky top-0 z-10 -mx-4 mb-4 flex gap-2 overflow-x-auto bg-page/90 px-4 py-2.5 backdrop-blur sm:-mx-6 sm:px-6 lg:hidden"
        aria-label="Sections"
      >
        {#each shown as s (s.name)}
          <a
            href="#{s.name}"
            class="shrink-0 rounded-full border px-3 py-1 text-xs transition {active === s.name
              ? 'border-accent bg-accent/15 text-text'
              : 'border-border text-muted'}">{s.title}</a
          >
        {/each}
      </nav>

      {#if failed}
        <p class="rounded-xl border border-error/40 bg-error/10 p-4 text-sm">{failed}</p>
      {:else if !sections.length}
        <p class="text-sm text-muted">Loading…</p>
      {:else if !shown.length}
        <p class="text-sm text-muted">No setting matches “{query}”.</p>
      {/if}

      <div class="space-y-6">
        {#each shown as s (s.name)}
          <section
            id={s.name}
            use:spy={s.name}
            class="scroll-mt-16 rounded-2xl border border-border bg-surface p-5 sm:p-6 lg:scroll-mt-6"
          >
            <h2 class="text-lg font-semibold">{s.title}</h2>
            {#if s.about}<p class="mt-0.5 text-sm text-muted first-letter:uppercase">{s.about}.</p>{/if}
            <div class="mt-3">
              {#each s.visible as o, i (o.name)}
                {#if o.group && o.group !== s.visible[i - 1]?.group}
                  <h3 class="mt-6 mb-1 text-xs font-semibold tracking-wider text-muted uppercase">{o.group}</h3>
                {/if}
                <OptionRow
                  option={o}
                  id={key(s.name, o.name)}
                  bind:value={values[key(s.name, o.name)]}
                  changed={!same(values[key(s.name, o.name)], saved[key(s.name, o.name)])}
                />
              {/each}
            </div>
            {#if s.folded}
              <button
                type="button"
                class="mt-3 flex items-center gap-1 text-sm text-accent hover:underline"
                onclick={() => (unfolded[s.name] = true)}
              >
                <Icon name="chevron" class="size-4" />
                Show {s.folded} advanced option{s.folded === 1 ? "" : "s"}
              </button>
            {/if}
          </section>
        {/each}
      </div>

      {#if sections.length}
        <a
          href="/settings/file"
          class="mt-6 flex items-center justify-between gap-4 rounded-2xl border border-border bg-surface p-5 transition hover:border-accent/60"
        >
          <div class="min-w-0">
            <p class="font-medium">Edit the config file yourself</p>
            <p class="truncate text-sm text-muted">{path}</p>
          </div>
          <Icon name="edit" class="size-5 shrink-0 text-muted" />
        </a>
      {/if}
    </main>
  </div>

  {#if changed.length}
    <div
      transition:fly={{ y: 24, duration: 180 }}
      class="fixed inset-x-3 bottom-20 z-30 mx-auto flex max-w-xl items-center gap-2 rounded-2xl border border-border bg-raised/95 p-2.5 pl-5 shadow-2xl backdrop-blur md:bottom-6"
    >
      <p class="flex-1 text-sm">{changed.length} unsaved change{changed.length === 1 ? "" : "s"}</p>
      <button type="button" class="rounded-lg px-3 py-2 text-sm text-muted hover:text-text" onclick={undo}>Undo</button>
      <button
        type="button"
        class="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-on-accent transition hover:brightness-110 disabled:opacity-60"
        disabled={saving}
        onclick={save}>{saving ? "Saving…" : "Save"}</button
      >
    </div>
  {/if}

  {#if notice}
    <div
      transition:fade={{ duration: 150 }}
      role="status"
      class="fixed top-4 left-1/2 z-40 -translate-x-1/2 rounded-xl border px-4 py-2 text-sm shadow-xl backdrop-blur {notice.error
        ? 'border-error/40 bg-error/15'
        : 'border-ok/40 bg-ok/15'}"
    >
      {notice.text}
    </div>
  {/if}
</Shell>
