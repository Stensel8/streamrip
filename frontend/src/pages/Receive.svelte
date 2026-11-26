<script lang="ts">
  import { post } from "../lib/api";

  // Where a login script moves its tab to: the login comes after the "#",
  // which never leaves the browser. It is taken out of the address (and the
  // history) and handed to the login that waits for it.
  let text = $state("Handing the login to streamrip…");
  let error = $state(false);

  (async () => {
    try {
      const given = JSON.parse(decodeURIComponent(location.hash.slice(1)));
      history.replaceState(null, "", location.pathname);
      await post(location.pathname.replace(/receive$/, "capture"), given);
      text = "Got it. You can close this tab and go back to streamrip.";
    } catch (e) {
      error = true;
      text =
        e instanceof SyntaxError
          ? "Run the script from the tab you logged in on: this page gets the login from it."
          : (e as Error).message;
    }
  })();
</script>

<div class="grid min-h-screen place-items-center p-6">
  <p class="card max-w-md text-sm {error ? 'border-error/40' : 'border-ok/40'}">{text}</p>
</div>
