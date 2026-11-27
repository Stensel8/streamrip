<script lang="ts">
  import type { Option, Value } from "./api";
  import Switch from "./Switch.svelte";

  let {
    option,
    id,
    value = $bindable(),
    changed,
  }: { option: Option; id: string; value: Value; changed: boolean } = $props();

  // A value the config has that its list does not name: still shown, as itself.
  const unlisted = $derived(option.choices.length > 0 && !option.choices.some(([v]) => v === value));

  // One or more of the choices, in the order they are tried: "tidal,deezer".
  const picked = $derived(
    String(value ?? "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
  );
  const pick = (choice: string) =>
    (value = (picked.includes(choice) ? picked.filter((p) => p !== choice) : [...picked, choice]).join(","));

  // A list is edited as "a, b, c", and taken when the box is left.
  function takeList(text: string) {
    value = text
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }
</script>

<!-- A switch stays beside its words; another control goes under them on a phone. -->
<div
  class="flex gap-3 border-t border-border py-4 first:border-t-0 sm:items-start sm:justify-between sm:gap-8 {option.kind ===
  'bool'
    ? 'items-start justify-between'
    : 'flex-col sm:flex-row'}"
>
  <div class="min-w-0 flex-1">
    <label for={id} class="flex items-center gap-2 text-sm font-medium">
      {option.label}
      {#if changed}<span class="size-1.5 rounded-full bg-accent" title="Changed, not saved yet"></span>{/if}
    </label>
    {#if option.help}<p class="mt-1 text-sm whitespace-pre-line text-muted">{option.help}</p>{/if}
  </div>

  {#if option.kind === "bool"}
    <Switch {id} label={option.label} checked={value === true} onchange={(on) => (value = on)} />
  {:else}
    <div class="sm:w-72 sm:shrink-0">
      {#if option.kind === "many"}
        <div {id} class="flex flex-wrap gap-2" role="group" aria-label={option.label}>
          {#each option.choices as [choice, text] (choice)}
            {@const at = picked.indexOf(String(choice))}
            <button
              type="button"
              aria-pressed={at >= 0}
              onclick={() => pick(String(choice))}
              class="inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition {at >= 0
                ? 'border-accent bg-accent/15 text-text'
                : 'border-border text-muted hover:text-text'}"
              >{#if at >= 0}<span class="font-semibold text-accent">{at + 1}</span>{/if}{text}</button
            >
          {/each}
        </div>
      {:else if option.choices.length}
        <select {id} bind:value class="w-full">
          {#if unlisted}<option {value}>{value}</option>{/if}
          {#each option.choices as [choice, text] (choice)}<option value={choice}>{text}</option>{/each}
        </select>
      {:else if option.kind === "int"}
        <input {id} type="number" bind:value class="w-full" />
      {:else if option.kind === "list"}
        <input
          {id}
          value={Array.isArray(value) ? value.join(", ") : ""}
          onchange={(e) => takeList(e.currentTarget.value)}
          placeholder="None"
          class="w-full"
        />
      {:else if option.kind === "secret"}
        <input {id} type="password" bind:value autocomplete="off" class="w-full" />
      {:else}
        <input {id} type="text" bind:value autocomplete="off" spellcheck="false" class="w-full" />
      {/if}
    </div>
  {/if}
</div>
