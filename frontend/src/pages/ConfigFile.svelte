<script lang="ts">
  import { api, post } from "../lib/api";
  import { say } from "../lib/notice.svelte";
  import PageHead from "../lib/ui/PageHead.svelte";

  let path = $state("");
  let text = $state("");
  let loaded = $state("");
  let error = $state("");
  let saving = $state(false);

  api<{ path: string; text: string }>("/settings/file/api").then(
    (file) => ([path, text, loaded] = [file.path, file.text, file.text]),
    (e: Error) => (error = e.message),
  );

  async function save() {
    saving = true;
    error = "";
    try {
      say((await post("/settings/file/api", { text })).message);
      loaded = text;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      saving = false;
    }
  }
</script>

<PageHead title="Config file">
  <code>{path}</code>, as it is. streamrip reads it through before saving it. The logins are not in it, but in
  state.toml next to it.
</PageHead>

{#if error}<p class="mb-4 rounded-xl border border-error/40 bg-error/10 p-4 text-sm whitespace-pre-line">{error}</p>{/if}

<div class="card">
  <textarea
    bind:value={text}
    rows="28"
    spellcheck="false"
    aria-label="The config file"
    class="min-h-[60vh] resize-y font-mono text-xs leading-relaxed"
  ></textarea>
  <div class="mt-4 flex flex-wrap justify-end gap-2">
    <a class="btn" href="/settings">Back to the settings</a>
    <button type="button" class="btn" disabled={text === loaded} onclick={() => (text = loaded)}>Undo</button>
    <button type="button" class="btn btn-primary" disabled={saving || text === loaded} onclick={save}
      >{saving ? "Saving…" : "Save"}</button
    >
  </div>
</div>
