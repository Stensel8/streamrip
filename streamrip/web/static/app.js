// What the pages do: start downloads, searches, transfers and logins, and show how they are getting on.
// Text from services (titles, names) only ever goes in with textContent, never innerHTML.
'use strict';

const $ = (id) => document.getElementById(id);
const el = (tag, props) => Object.assign(document.createElement(tag), props);

// A request to our own server. Asking for JSON makes it answer errors as JSON too, instead of as a page.
async function api(url, init = {}) {
  const resp = await fetch(url, { ...init, headers: { Accept: 'application/json', ...init.headers } });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.message);
  return data;
}

const postJson = (body) => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const PHASES = { read: 'Read', match: 'Match', check: 'Compare', add: 'Add', find: 'Look up', download: 'Download', login: 'Log in' };
const pageTitle = document.title;

// Jobs the person closed. Kept for this tab, so a reload does not bring one back.
const dismissed = new Set();
try {
  for (const id of JSON.parse(sessionStorage.getItem('dismissed') || '[]')) dismissed.add(id);
} catch {
  // No storage: a dismissed job just comes back after a reload.
}
function dismiss(id) {
  dismissed.add(id);
  try {
    sessionStorage.setItem('dismissed', JSON.stringify([...dismissed].slice(-20)));
  } catch {
    // Same.
  }
  $('job').hidden = true;
  document.title = pageTitle;
  busy(false);
}
let shown = null; // the id of the job in the panel
const count = (n) => n.toLocaleString();
const percent = (done, total) => Math.floor((100 * done) / Math.max(total, 1));

// 72 seconds as "1:12".
function clock(seconds) {
  const s = Math.round(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// Time left, rounded the way people say it.
const roughly = (seconds) => (seconds < 60 ? `${Math.max(10, Math.round(seconds / 10) * 10)} s` : `${Math.round(seconds / 60)} min`);

// A job runs one at a time from a page: its buttons are off until it ends.
function busy(on) {
  for (const button of document.querySelectorAll('[data-job-button]')) button.disabled = on;
}

// A plain message in the job panel, for a job that is starting or could not start.
function show(text, { error = false } = {}) {
  $('job').hidden = false;
  $('job-title').textContent = error ? 'Something went wrong' : 'Starting';
  for (const id of ['job-clock', 'job-numbers']) $(id).textContent = '';
  for (const id of ['job-steps', 'job-current', 'job-unmatched', 'job-download', 'job-rows', 'job-log', 'job-login']) {
    if ($(id)) $(id).hidden = true; // the login parts are on the Accounts page only
  }
  $('job-bar').hidden = error;
  $('job-bar').removeAttribute('value'); // no value: a bar that moves without saying how far
  $('job-text').textContent = text;
  $('job-text').classList.toggle('error', error);
}

// One track that was not found, and why.
function miss({ track, why }) {
  const item = el('li');
  item.append(track, el('span', { className: 'muted', textContent: ` — ${why}` }));
  return item;
}

// One track downloading: its name and a bar.
function row({ desc, done, total, eta }) {
  const bar = el('progress');
  if (total) {
    bar.max = total;
    bar.value = Math.min(done, total);
  }
  const item = el('li');
  item.append(el('span', { textContent: eta === null ? desc : `${desc} · about ${roughly(eta)} left` }), bar);
  return item;
}

function render(id, job) {
  if (dismissed.has(id)) return;
  shown = id;
  const running = job.status === 'running';
  const failed = job.status === 'error';
  $('job').hidden = false;
  $('job-title').textContent = job.title;
  $('job-clock').textContent = clock(job.elapsed);

  // The row of steps: the ones before the current step are done.
  const at = running ? Math.max(job.phases.indexOf(job.phase), 0) : job.phases.indexOf(job.phase);
  $('job-steps').hidden = job.phases.length < 2;
  $('job-steps').replaceChildren(
    ...job.phases.map((phase, i) => {
      const state = job.status === 'done' || i < at ? 'done' : i === at ? (failed ? 'failed' : 'current') : '';
      return el('li', { textContent: PHASES[phase] ?? phase, className: state });
    }),
  );

  $('job-text').textContent = running ? job.text : job.message;
  $('job-text').classList.toggle('error', failed);

  // A login: the page to log in on, and a box for the address a browser elsewhere ends up on.
  if ($('job-login')) {
    $('job-login').hidden = !(running && job.link);
    $('job-link').href = job.link || '#';
    $('job-paste-box').hidden = !(running && job.pasteable);
    $('job-cancel').hidden = !(running && (job.pasteable || job.script));
    $('job-script').hidden = !(running && job.script);
    if (job.script && $('job-script-text').value !== job.script) $('job-script-text').value = job.script;
    $('job-paste').dataset.job = id;
    $('job-cancel').dataset.job = id;
  }

  const bar = $('job-bar');
  bar.hidden = !running; // a login that waits slides too: it has no end in sight
  if (job.total === null) {
    bar.removeAttribute('value');
  } else {
    bar.max = Math.max(job.total, 1);
    bar.value = job.total ? job.done : 1;
  }

  const unit = job.phase === 'download' ? ' tracks' : '';
  const facts = [];
  if (running && job.total === null && job.done) facts.push(`${count(job.done)} so far`);
  if (running && job.total !== null) facts.push(`${count(job.done)} of ${count(job.total)}${unit}`, `${percent(job.done, job.total)}%`);
  if (running && job.eta !== null) facts.push(`about ${roughly(job.eta)} left`);
  if (job.found || job.not_found) facts.push(`${count(job.found)} found`, `${count(job.not_found)} not found`);
  if (!running) facts.push(`took ${clock(job.elapsed)}`);
  $('job-numbers').textContent = facts.join(' · ');
  $('job-current').hidden = !(running && job.phase === 'match');
  $('job-current').textContent = job.current;

  $('job-rows').hidden = !(running && job.rows.length);
  $('job-rows').replaceChildren(...job.rows.map(row));

  $('job-unmatched').hidden = !job.not_found;
  $('job-unmatched-title').textContent = `${running ? 'Not found so far' : 'Not found'} (${count(job.not_found)})`;
  $('job-unmatched-list').replaceChildren(...job.unmatched.map(miss));
  const more = job.not_found - job.unmatched.length;
  $('job-unmatched-more').textContent = more > 0 ? `… and ${count(more)} more in the CSV` : '';
  $('job-unmatched-link').href = `/jobs/${id}/unmatched.csv`;
  $('job-unmatched-link').hidden = running; // the list is complete only at the end

  $('job-download').hidden = !job.download;
  $('job-download-link').href = `/jobs/${id}/download`;
  $('job-report').hidden = !job.report;
  $('job-report-link').href = `/jobs/${id}/report.csv`;

  $('job-log').hidden = !job.log.length;
  $('job-log-lines').replaceChildren(...job.log.map(({ level, text }) => el('li', { textContent: text, className: level })));
  if ((failed || job.kind === 'verify' || job.kind === 'plan') && job.log.length) $('job-log').open = true;

  // The browser tab says how far it is, so it can be followed from another tab.
  const phase = PHASES[job.phase] ?? 'Starting';
  const progress = job.total ? `${phase} ${percent(job.done, job.total)}%` : `${phase}…`;
  document.title = `${running ? progress : failed ? 'Failed' : 'Done'} · ${pageTitle}`;
}

// Poll a background job until it ends. Returns what it ended as.
async function follow(id, { opened = false } = {}) {
  for (;;) {
    const job = await api(`/jobs/${id}`);
    render(id, job);
    if (dismissed.has(id)) return job;
    if (job.link && !opened && job.kind === 'login') {
      opened = true;
      window.open(job.link, '_blank', 'noopener');
    }
    if (job.status !== 'running') return job;
    await new Promise((resolve) => setTimeout(resolve, job.kind === 'login' ? 1000 : 500));
  }
}

// Follow a background job to the end, with the buttons off meanwhile.
async function track(id, options) {
  busy(true);
  try {
    const job = await follow(id, options);
    if (job.download) window.location.href = `/jobs/${id}/download`; // saves the file; the page stays
    if (job.kind === 'login' && job.status === 'done') setTimeout(() => window.location.reload(), 1500);
  } catch (err) {
    show(err.message, { error: true });
    document.title = pageTitle;
  } finally {
    busy(false);
  }
}

// Start a background job and follow it to the end.
async function start(url, init) {
  busy(true);
  show('Starting…');
  $('job').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  try {
    const { id } = await api(url, { method: 'POST', ...init });
    await track(id);
  } catch (err) {
    show(err.message, { error: true });
    document.title = pageTitle;
    busy(false);
  }
}

// A page that was reloaded while a job runs goes on showing it, and does not offer to start another.
async function resume() {
  try {
    const { id } = await api(`/jobs/running?kinds=${encodeURIComponent($('job').dataset.kinds || '')}`);
    if (id && !dismissed.has(id)) await track(id, { opened: true }); // a login's page was opened already
  } catch {
    // Not knowing is fine: the page just starts empty.
  }
}
if ($('job')) resume();

// A form sent as JSON: its fields by name, and the button that sent it (Preview says plan=1).
const fields = (form, submitter) => Object.fromEntries(new FormData(form, submitter));
// The same, as a form with its files.
const upload = (form, submitter) => ({ body: new FormData(form, submitter) });
function onSubmit(id, handler) {
  $(id)?.addEventListener('submit', (e) => {
    e.preventDefault();
    handler(e.target, e.submitter);
  });
}

// --- the download page -------------------------------------------------------------------------------

onSubmit('urls-form', (form, who) => start('/download', upload(form, who)));
onSubmit('csv-form', (form, who) => start('/download/csv', upload(form, who)));
onSubmit('id-form', (form, who) => start('/download/id', postJson(fields(form, who))));
onSubmit('lastfm-form', (form, who) => start('/download/lastfm', postJson(fields(form, who))));

// One search result, to tick.
function result(r, i) {
  const box = el('input', { type: 'checkbox', value: String(i) });
  const cover = r.image ? el('img', { src: r.image, alt: '', loading: 'lazy' }) : el('span', { className: 'no-cover' });
  const what = el('span', { className: 'what' });
  const notes = [r.artist, r.year, r.explicit ? 'explicit' : null].filter(Boolean).join(' · ');
  what.append(el('strong', { textContent: r.name }), el('span', { textContent: notes }));
  const details = r.details.map(([label, value]) => `${label}: ${value}`).join(' · ');
  if (details) what.append(el('span', { textContent: details }));
  const label = el('label');
  label.append(box, cover, what);
  const item = el('li');
  item.append(label);
  return item;
}

// A source that cannot be searched for every kind only offers the ones it can.
const searchForm = $('search-form');
function offerKinds() {
  const kinds = searchForm.source.selectedOptions[0].dataset.kinds.split(' ');
  for (const option of searchForm.media_type.options) option.hidden = option.disabled = !kinds.includes(option.value);
  if (searchForm.media_type.selectedOptions[0].disabled) searchForm.media_type.value = kinds.includes('album') ? 'album' : kinds[0];
}
searchForm?.source.addEventListener('change', offerKinds);
if (searchForm) offerKinds();

let found = [];
onSubmit('search-form', async (form) => {
  const status = $('search-status');
  status.hidden = false;
  status.classList.remove('error');
  status.textContent = 'Searching…';
  $('search-results').hidden = $('search-actions').hidden = true;
  $('search-button').disabled = true;
  try {
    found = await api('/search', postJson(fields(form)));
    status.textContent = found.length ? `${found.length} found. Tick what to download.` : 'Nothing found.';
    $('search-results').replaceChildren(...found.map(result));
    $('search-results').hidden = $('search-actions').hidden = !found.length;
  } catch (err) {
    status.textContent = err.message;
    status.classList.add('error');
  } finally {
    $('search-button').disabled = false;
  }
});

function downloadChosen(plan) {
  const chosen = [...$('search-results').querySelectorAll('input:checked')].map((box) => found[Number(box.value)]);
  if (!chosen.length) {
    show('Tick one or more results first.', { error: true });
    return;
  }
  const items = chosen.map(({ source, media_type, id }) => ({ source, media_type, id }));
  start('/download', postJson({ items, plan }));
}
$('search-download')?.addEventListener('click', () => downloadChosen(false));
$('search-preview')?.addEventListener('click', () => downloadChosen(true));

// --- the playlist page ---------------------------------------------------------------------------------

// Fill a playlist picker with the readable playlists of a service. Empty ones cannot be picked.
async function fillPlaylists(service, select) {
  select.replaceChildren(new Option('Liked songs', ''));
  if (select.hasAttribute('data-all')) select.add(new Option('Everything: liked songs and every playlist', '*'));
  if (!service) return;
  try {
    for (const p of await api(`/playlists/${service}/list`)) {
      if (!p.readable) continue;
      const empty = p.track_count === 0;
      const option = new Option(`${p.name} (${empty ? 'empty' : (p.track_count ?? '?')})`, p.id);
      option.disabled = empty;
      select.add(option);
    }
  } catch (err) {
    show(err.message, { error: true });
  }
}

for (const [service, playlist] of [
  ['export-service', 'export-playlist'],
  ['transfer-source', 'transfer-playlist'],
  ['pl-download-service', 'pl-download-playlist'],
]) {
  $(service)?.addEventListener('change', (e) => fillPlaylists(e.target.value, $(playlist)));
}

onSubmit('export-form', (form) => {
  const { service, ...rest } = fields(form);
  start(`/playlists/${service}/export`, postJson(rest));
});
onSubmit('import-form', (form) => start(`/playlists/${new FormData(form).get('service')}/import`, { body: new FormData(form) }));
onSubmit('transfer-form', (form) => start('/playlists/transfer', postJson(fields(form))));
onSubmit('pl-download-form', (form, who) => {
  const { service, ...rest } = fields(form, who);
  start(`/playlists/${service}/download`, postJson(rest));
});

// --- the accounts page ------------------------------------------------------------------------------------

$('verify')?.addEventListener('click', () => start('/accounts/verify'));
$('tidal-login')?.addEventListener('click', () => start('/accounts/tidal/login'));
$('qobuz-script')?.addEventListener('click', () => start('/accounts/qobuz/script'));

// The page the Qobuz script moves its tab to: the token comes after the "#", which never
// leaves the browser. It is taken out of the address (and the history) and handed over.
async function receiveQobuz() {
  const text = $('qobuz-receive-text');
  try {
    const given = JSON.parse(decodeURIComponent(location.hash.slice(1)));
    history.replaceState(null, '', location.pathname);
    await api('/accounts/qobuz/capture', postJson(given));
    text.textContent = 'Got it. You can close this tab and go back to streamrip.';
  } catch (err) {
    text.textContent = err instanceof SyntaxError ? 'Run the script from the Qobuz tab: this page gets the token from it.' : err.message;
  }
}
if ($('qobuz-receive')) receiveQobuz();
$('job-script-copy')?.addEventListener('click', async (e) => {
  const box = $('job-script-text');
  box.select();
  try {
    await navigator.clipboard.writeText(box.value);
    e.target.textContent = 'Copied';
  } catch {
    e.target.textContent = 'Press Ctrl+C to copy';
  }
});
onSubmit('soloist-form', (form) => start('/accounts/soloist/pair', postJson(fields(form))));
for (const button of document.querySelectorAll('[data-browser-login]')) {
  button.addEventListener('click', () => start(`/accounts/${button.dataset.browserLogin}/login`));
}

$('job-dismiss')?.addEventListener('click', () => {
  if (!shown) return $('job').setAttribute('hidden', '');
  const id = shown;
  // A login that waits holds a port: giving up on it frees that.
  if (!$('job-cancel')?.hidden) api(`/jobs/${id}/cancel`, { method: 'POST' }).catch(() => {});
  dismiss(id);
});
$('job-cancel')?.addEventListener('click', (e) => api(`/jobs/${e.target.dataset.job}/cancel`, { method: 'POST' }).catch(() => {}));

onSubmit('job-paste', async (form) => {
  try {
    await api(`/jobs/${form.dataset.job}/paste`, postJson(fields(form)));
    form.reset();
  } catch (err) {
    $('job-text').textContent = err.message;
    $('job-text').classList.add('error');
  }
});

// Copy a redirect URI with one click.
for (const button of document.querySelectorAll('[data-copy]')) {
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      button.textContent = 'Copied';
    } catch {
      button.textContent = 'Select it and copy it yourself';
    }
    setTimeout(() => (button.textContent = 'Copy'), 2000);
  });
}

// --- the library page ------------------------------------------------------------------------------------

$('repair')?.addEventListener('click', () => start('/library/repair', postJson({ flat: $('repair-flat').checked })));

// A form that asks before it forgets.
for (const form of document.querySelectorAll('form[data-confirm]')) {
  form.addEventListener('submit', (e) => {
    if (!confirm(form.dataset.confirm)) e.preventDefault();
  });
}
