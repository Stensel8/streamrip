<script lang="ts">
  let { tone = "plain", text }: { tone?: "ok" | "free" | "warn" | "error" | "plain"; text: string } = $props();
  const tones = {
    ok: "border-ok/40 bg-ok/10 text-ok",
    free: "border-accent/40 bg-accent/10 text-accent",
    warn: "border-warn/40 bg-warn/10 text-warn",
    error: "border-error/40 bg-error/10 text-error",
    plain: "border-border text-muted",
  };
</script>

<span class="inline-flex shrink-0 items-center rounded-full border px-2.5 py-0.5 text-xs font-medium {tones[tone]}"
  >{text}</span
>
