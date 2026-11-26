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
      {#if option.choices.length}
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
