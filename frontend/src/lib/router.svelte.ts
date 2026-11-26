// Which page is shown: the address's path. A link to another page of the app
// changes it without loading the page again, so a job that runs stays in view.

export const PAGES = ["/", "/playlists", "/accounts", "/library", "/settings", "/settings/file"];

export const route = $state({ path: location.pathname });

export function go(path: string) {
  history.pushState(null, "", path);
  route.path = location.pathname;
  scrollTo(0, 0);
}

addEventListener("popstate", () => (route.path = location.pathname));

addEventListener("click", (e) => {
  const link = (e.target as Element | null)?.closest?.("a");
  if (!link || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  if (link.target || link.hasAttribute("download")) return;
  const url = new URL(link.href);
  // Another site, a file (a CSV), or a section of this page: the browser's own way.
  if (url.origin !== location.origin || !PAGES.includes(url.pathname)) return;
  if (url.pathname === location.pathname && url.hash) return;
  e.preventDefault();
  go(url.pathname);
});
