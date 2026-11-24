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
    $(id).hidden = true;
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
function row({ desc, done, total }) {
  const bar = el('progress');
  if (total) {
    bar.max = total;
    bar.value = Math.min(done, total);
  }
  const item = el('li');
  item.append(el('span', { textContent: desc }), bar);
  return item;
}

function render(id, job) {
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
  $('job-login').hidden = !(running && job.link);
  $('job-link').href = job.link || '#';
  $('job-paste-box').hidden = $('job-cancel').hidden = !(running && job.pasteable);
  $('job-paste').dataset.job = id;
  $('job-cancel').dataset.job = id;

  const bar = $('job-bar');
  bar.hidden = !running || job.kind === 'login';
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

  $('job-log').hidden = !job.log.length;
  $('job-log-lines').replaceChildren(...job.log.map(({ level, text }) => el('li', { textContent: text, className: level })));
  if (failed && job.log.length) $('job-log').open = true;

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
    const { id } = await api('/jobs/running');
    if (id) await track(id, { opened: true }); // a login's page was opened already
  } catch {
    // Not knowing is fine: the page just starts empty.
  }
}
if ($('job')) resume();

// A form sent as JSON: its fields by name.
const fields = (form) => Object.fromEntries(new FormData(form));
function onSubmit(id, handler) {
  $(id)?.addEventListener('submit', (e) => {
    e.preventDefault();
    handler(e.target);
  });
}

// --- the download page -------------------------------------------------------------------------------

onSubmit('urls-form', (form) => start('/download', postJson(fields(form))));
onSubmit('csv-form', (form) => start('/download/csv', { body: new FormData(form) }));

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

$('search-download')?.addEventListener('click', () => {
  const chosen = [...$('search-results').querySelectorAll('input:checked')].map((box) => found[Number(box.value)]);
  if (!chosen.length) {
    show('Tick one or more results first.', { error: true });
    return;
  }
  start('/download', postJson({ items: chosen.map(({ source, media_type, id }) => ({ source, media_type, id })) }));
});

// --- the playlist page ---------------------------------------------------------------------------------

// Fill a playlist picker with the readable playlists of a service. Empty ones cannot be picked.
async function fillPlaylists(service, select) {
  select.replaceChildren(new Option('Liked songs', ''));
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
  const { service, playlist } = fields(form);
  start(`/playlists/${service}/export`, postJson({ playlist: playlist || null }));
});
onSubmit('import-form', (form) => start(`/playlists/${new FormData(form).get('service')}/import`, { body: new FormData(form) }));
onSubmit('transfer-form', (form) => start('/playlists/transfer', postJson(fields(form))));
onSubmit('pl-download-form', (form) => {
  const { service, ...rest } = fields(form);
  start(`/playlists/${service}/download`, postJson(rest));
});

// --- the accounts page ------------------------------------------------------------------------------------

$('tidal-login')?.addEventListener('click', () => start('/accounts/tidal'));
for (const button of document.querySelectorAll('[data-browser-login]')) {
  button.addEventListener('click', () => start(`/accounts/${button.dataset.browserLogin}/login`));
}

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
