"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const view = $("#view");
let settingsCache = null;
let ME = null;
const isAdmin = () => !!ME && ME.role === "admin";
const canUhd = () => !!ME && (ME.role === "admin" || !!ME.allow_uhd);
// have = versions already in the library: only the missing ones are offered
function tierOptions(long = true, have = []) {
  const opts = [["hd", "1080p"], ...(canUhd() ? [["uhd", "4K"], ["both", "1080p + 4K"]] : [])]
    .filter(([v]) => !(v === "both" ? have.length : have.includes(v)));
  return opts.map(([v, l]) => `<option value="${v}">${l}${long && v !== "both" ? " library" : ""}</option>`).join("");
}
const TIER_LABEL = { hd: "1080p", uhd: "4K", both: "1080p + 4K", auto: "by file" };
// one entry per title, its versions (1080p / 4K) sorted
function groupVersions(rows, key) {
  const m = new Map();
  rows.forEach((r) => { const k = key(r); if (!m.has(k)) m.set(k, []); m.get(k).push(r); });
  return [...m.values()].map((vs) => vs.sort((a, b) => (a.tier < b.tier ? -1 : 1)));
}

async function api(path, opts = {}) {
  const o = { headers: { "Content-Type": "application/json" }, ...opts };
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  const r = await fetch("/api" + path, o);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (r.status === 401 && data && data.login) { ME = null; renderAuth("login"); }
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}
function toast(msg, err = false) {
  const d = document.createElement("div");
  d.textContent = msg; if (err) d.className = "err";
  $("#toast").appendChild(d); setTimeout(() => d.remove(), 5000);
}
async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }
function size(b) { if (!b) return ""; const g = b / 1073741824; return g >= 1 ? g.toFixed(2) + " GB" : (b / 1048576).toFixed(0) + " MB"; }
function tierChip(t) { return t === "uhd" ? '<span class="chip uhd">4K</span>' : t === "hd" ? '<span class="chip hd">1080p</span>' : `<span class="chip">${esc(TIER_LABEL[t] || t)}</span>`; }
// movie version status: "4K · Bluray-2160p" / "1080p · Missing" / "4K · downloading 40%"
function movieVerChip(m) {
  const st = m.download ? [`warn`, `${m.download.status} ${Math.round(m.download.progress)}%`] : m.file_path ? ["ok", m.file_quality] : m.monitored ? ["bad", "Missing"] : ["", "Unmonitored"];
  return `<span class="chip ${st[0]}">${TIER_LABEL[m.tier]} · ${esc(st[1])}</span>`;
}
// versions bar on a title page: switch between versions, or add the missing one
function versionBar(kind, item) {
  const page = kind === "movie" ? "movie" : "show";
  return `<div class="chips versions"><span class="small muted">Versions:</span>${["hd", "uhd"].map((t) => {
    const v = item.versions.find((x) => x.tier === t);
    if (v) {
      const done = v.total && v.have >= v.total;
      const st = kind === "movie" ? (v.have ? esc(v.file_quality) : "Missing") : `${v.have}/${v.total}`;
      return `<a class="chip ${t} ${v.id === item.id ? "current" : ""}" href="#/${page}/${v.id}" title="${v.id === item.id ? "Showing this version" : "Open this version"}">${TIER_LABEL[t]} · ${st}${done ? " ✓" : ""}</a>`;
    }
    return isAdmin() && (t === "hd" || canUhd()) ? `<button class="small" onclick="addVersion('${kind}', '${t}', this)">+ Add ${TIER_LABEL[t]} version</button>` : "";
  }).join("")}</div>`;
}
async function addVersion(kind, tier, btn) {
  const it = window._item; btn.disabled = true;
  await guard(async () => {
    const r = kind === "movie"
      ? await api("/movies", { method: "POST", body: { tmdb_id: it.tmdb_id, tier, search: true } })
      : await api("/series", { method: "POST", body: { tmdb_id: it.tmdb_id || undefined, tvdb_id: it.tvdb_id || undefined, tier, monitor: "same", search: true } });
    toast(`Added the ${TIER_LABEL[tier]} version — searching`);
    location.hash = `#/${kind === "movie" ? "movie" : "show"}/${r.added[0].id}`;
  });
  btn.disabled = false;
}
async function deleteVersion(kind, item) {
  const others = item.versions.filter((v) => v.id !== item.id);
  const what = others.length ? `the ${TIER_LABEL[item.tier]} version of ${item.title}` : item.title;
  if (!confirm(`Remove ${what}?`)) return;
  const files = confirm(others.length ? `Also delete the ${TIER_LABEL[item.tier]} files from disk? The ${TIER_LABEL[others[0].tier]} version and its files stay.` : "Also delete its files from disk?");
  await api(`/${kind === "movie" ? "movies" : "series"}/${item.id}?delete_files=${files}`, { method: "DELETE" });
  location.hash = others.length ? `#/${kind === "movie" ? "movie" : "show"}/${others[0].id}` : `#/${kind === "movie" ? "movies" : "series"}`;
}
function poster(url, title) { return url ? `<img loading="lazy" src="${esc(url)}" alt="">` : `<div class="noposter">${esc(title)}</div>`; }
function modal(html) { $("#modal .modal-body").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

// ------------------------------------------------------------- router ---
const routes = { requests: (sub) => renderRequests(sub), discover: renderDiscover, collections: renderCollections, import: renderImport, movies: renderMovies, series: renderSeries, add: renderAdd, activity: renderActivity, settings: renderSettings, movie: renderMovie, show: renderShow };
const ADMIN_PAGES = ["settings", "activity", "import"];
async function route() {
  let [, name = "movies", arg] = location.hash.split("/");
  if (!isAdmin() && ADMIN_PAGES.includes(name)) { location.hash = "#/movies"; return; }
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === name || ((name === "movie" || name === "collections") && a.dataset.tab === "movies") || (name === "show" && a.dataset.tab === "series")));
  view.innerHTML = '<p class="muted">Loading…</p>';
  await guard(() => (routes[name] || renderMovies)(arg));
}
window.addEventListener("hashchange", () => { if (ME) route(); });

async function checkBanner() {
  if (!isAdmin()) { $("#banner").innerHTML = ""; return; }
  const st = await api("/status").catch(() => null);
  const s = await api("/settings").catch(() => null);
  settingsCache = s;
  const msgs = [];
  if (st && !st.trash_loaded) msgs.push('TRaSH Guides data not downloaded yet — <a href="#/settings">Settings → Profiles</a> → "Sync TRaSH Guides now".');
  if (s && !s.tmdb_api_key) msgs.push('Add your free TMDB API key in <a href="#/settings">Settings → Metadata</a> — it powers movies, TV and the Streaming tab.');
  $("#banner").innerHTML = msgs.join(" ");
}

// ------------------------------------------------------------- movies ---
async function renderMovies() {
  const movies = groupVersions(await api("/movies"), (m) => m.tmdb_id);
  view.innerHTML = `<div class="toolbar"><h2 class="grow">Movies <span class="muted small">(${movies.length})</span></h2>
    <input id="flt" placeholder="Filter…"><select id="tierf"><option value="">All tiers</option><option value="hd">1080p</option><option value="uhd">4K</option></select>
    <select id="statf"><option value="">Any status</option><option value="missing">Missing</option><option value="have">Downloaded</option></select>
    <a href="#/collections"><button>Collections</button></a>
    <a href="#/add"><button class="primary">Add movie</button></a></div><div class="grid" id="g"></div>`;
  const draw = () => {
    const f = $("#flt").value.toLowerCase(), t = $("#tierf").value, st = $("#statf").value;
    $("#g").innerHTML = movies.filter((vs) => (!f || vs[0].title.toLowerCase().includes(f)) && (!t || vs.some((m) => m.tier === t))
        && (!st || (st === "have" ? vs.some((m) => m.file_path) : vs.some((m) => !m.file_path && m.monitored))))
      .map((vs) => { const m = vs[0]; return `<div class="card" onclick="location.hash='#/movie/${m.id}'">${poster(m.poster, m.title)}
        <div class="meta"><div class="title" title="${esc(m.title)}">${esc(m.title)}</div>
        <div class="chips"><span class="chip">${m.year || ""}</span>${vs.map(movieVerChip).join("")}</div></div></div>`; }).join("") || '<p class="muted">Nothing here yet.</p>';
  };
  ["flt", "tierf", "statf"].forEach((id) => $("#" + id).addEventListener("input", draw));
  draw();
}

async function renderMovie(id) {
  const m = await api("/movies/" + id);
  window._item = m;
  const multi = m.versions.length > 1;
  view.innerHTML = `<div class="detail">${m.poster ? `<img src="${esc(m.poster)}" alt="">` : ""}<div class="info">
    <h2>${esc(m.title)} <span class="muted">(${m.year || "?"})</span></h2>
    <div class="chips">${m.runtime ? `<span class="chip">${m.runtime} min</span>` : ""}
      <a class="chip" target="_blank" rel="noopener" href="https://www.themoviedb.org/movie/${m.tmdb_id}">TMDB</a>
      ${m.imdb_id ? `<a class="chip" target="_blank" rel="noopener" href="https://www.imdb.com/title/${esc(m.imdb_id)}">IMDb</a>` : ""}
      ${m.collection_id ? `<a class="chip" href="javascript:void(0)" onclick="openCollection('collection', ${m.collection_id})">Part of ${esc(m.collection_name)} →</a>` : ""}</div>
    <p>${esc(m.overview)}</p>
    ${versionBar("movie", m)}
    ${isAdmin() ? `<p class="small muted">Folder: <code>${esc(m.path)}</code></p>` : ""}
    <p class="small"><b>${TIER_LABEL[m.tier]} version:</b> ${m.file_path ? `${isAdmin() ? `File: <code>${esc(m.file_path)}</code> ` : "Downloaded "}<span class="chip ok">${esc(m.file_quality)}</span> <span class="chip">score ${m.file_score}</span> ${size(m.file_size)}` : '<span class="chip bad">Not downloaded yet</span>'}</p>
    <div class="toolbar admin-only">
      <button class="primary" id="as">Automatic search</button><button id="is">Interactive search</button>
      ${multi ? "" : '<select id="tier" title="Move to the other library"><option value="hd">1080p library</option><option value="uhd">4K library</option></select>'}
      <label><input type="checkbox" id="mon" ${m.monitored ? "checked" : ""}> Monitored</label>
      <button id="rs">Rescan folder</button>
      <button class="danger" id="del">${multi ? `Remove ${TIER_LABEL[m.tier]} version` : "Delete"}</button></div>
    <div class="admin-only"><h3>History</h3>${historyTable(m.history)}</div></div></div>`;
  if ($("#tier")) {
    $("#tier").value = m.tier;
    $("#tier").onchange = () => guard(async () => { await api("/movies/" + id, { method: "PUT", body: { tier: $("#tier").value } }); toast("Library changed"); route(); });
  }
  $("#mon").onchange = () => guard(() => api("/movies/" + id, { method: "PUT", body: { monitored: $("#mon").checked } }));
  $("#as").onclick = () => guard(async () => { $("#as").disabled = true; const r = await api(`/movies/${id}/search`, { method: "POST" }); toast(r.some((x) => x.accepted) ? "Release grabbed" : `No acceptable release (${r.length} found)`); route(); });
  $("#is").onclick = () => interactive("movie", id, `/movies/${id}/search?interactive=true`);
  $("#rs").onclick = () => rescan(`/movies/${id}/rescan`);
  $("#del").onclick = () => guard(() => deleteVersion("movie", m));
}

function historyTable(rows) {
  if (!rows || !rows.length) return '<p class="muted">Nothing yet.</p>';
  return `<div class="table-wrap"><table><tr><th>Release</th><th>Quality</th><th>Score</th><th>Status</th><th>Added</th></tr>
  ${rows.map((d) => `<tr><td>${esc(d.release_title)}<div class="small muted">${esc(d.indexer)} · ${size(d.size)}</div>${d.message ? `<div class="rej">${esc(d.message)}</div>` : ""}</td><td>${esc(d.quality)}</td><td>${d.score}</td><td>${statusChip(d)}</td><td class="small muted">${esc(d.added)}</td></tr>`).join("")}</table></div>`;
}
function statusChip(d) {
  const cls = { imported: "ok", failed: "bad", import_failed: "bad", downloading: "warn", queued: "", importing: "warn" }[d.status] ?? "";
  const pct = d.status === "downloading" ? ` ${Math.round(d.progress)}%` : "";
  return `<span class="chip ${cls}">${esc(d.status.replace("_", " "))}${pct}</span>`;
}

async function interactive(kind, itemId, path) {
  modal('<h2>Interactive search</h2><p class="muted">Searching indexers…</p>');
  const rows = await guard(() => api(path, { method: "POST" }));
  if (!rows) return closeModal();
  window._results = rows;
  $("#modal .modal-body").innerHTML = `<div class="toolbar"><h2 class="grow">Interactive search <span class="muted small">${rows.length} results</span></h2><button onclick="closeModal()">Close</button></div>
  <div class="table-wrap"><table><tr><th>Release</th><th>Quality</th><th>Score</th><th>Size</th><th>Age</th><th></th></tr>
  ${rows.map((r, i) => `<tr><td>${esc(r.title)}<div class="small muted">${esc(r.indexer)}${r.custom_formats.length ? " · " + r.custom_formats.map(esc).join(", ") : ""}</div>
    ${r.reasons.map((x) => `<div class="rej">${esc(x)}</div>`).join("")}</td>
    <td>${esc(r.quality)}</td><td>${r.score}</td><td>${size(r.size)}</td><td>${r.age_days ?? ""}d</td>
    <td><button class="small ${r.accepted ? "primary" : ""}" onclick="grabRow('${kind}', ${itemId}, ${i}, this)">Grab</button></td></tr>`).join("")}</table></div>`;
}
async function grabRow(kind, itemId, i, btn) {
  const r = window._results[i];
  btn.disabled = true;
  await guard(async () => { await api("/grab", { method: "POST", body: { kind, item_id: itemId, guid: r.guid, title: r.title, link: r.link, indexer: r.indexer, indexer_id: r.indexer_id, size: r.size, episode_ids: r.episode_ids } }); toast("Sent to NZBGet"); btn.textContent = "Sent"; });
}

// ------------------------------------------------------------- series ---
async function renderSeries() {
  const series = groupVersions(await api("/series"), (s) => (s.tmdb_id ? "tmdb" + s.tmdb_id : "tvdb" + s.tvdb_id));
  view.innerHTML = `<div class="toolbar"><h2 class="grow">TV <span class="muted small">(${series.length})</span></h2>
    <input id="flt" placeholder="Filter…"><select id="tierf"><option value="">All tiers</option><option value="hd">1080p</option><option value="uhd">4K</option></select>
    <a href="#/add"><button class="primary">Add series</button></a></div><div class="grid" id="g"></div>`;
  const draw = () => {
    const f = $("#flt").value.toLowerCase(), t = $("#tierf").value;
    $("#g").innerHTML = series.filter((vs) => (!f || vs[0].title.toLowerCase().includes(f)) && (!t || vs.some((s) => s.tier === t)))
      .map((vs) => { const s = vs[0]; return `<div class="card" onclick="location.hash='#/show/${s.id}'">${poster(s.poster, s.title)}
        <div class="meta"><div class="title" title="${esc(s.title)}">${esc(s.title)}</div>
        <div class="chips">${vs.map((v) => `<span class="chip ${v.have >= v.aired && v.aired ? "ok" : ""}">${TIER_LABEL[v.tier]} · ${v.have}/${v.aired}</span>`).join("")}${s.status ? `<span class="chip">${esc(s.status)}</span>` : ""}</div></div></div>`; }).join("") || '<p class="muted">Nothing here yet.</p>';
  };
  ["flt", "tierf"].forEach((id) => $("#" + id).addEventListener("input", draw));
  draw();
}

async function renderShow(id) {
  const s = await api("/series/" + id);
  window._item = s;
  const multi = s.versions.length > 1;
  const seasons = {};
  s.episodes.forEach((e) => (seasons[e.season] = seasons[e.season] || []).push(e));
  const today = new Date().toISOString().slice(0, 10);
  view.innerHTML = `<div class="detail">${s.poster ? `<img src="${esc(s.poster)}" alt="">` : ""}<div class="info">
    <h2>${esc(s.title)} <span class="muted">(${s.year || "?"})</span></h2>
    <div class="chips">${s.status ? `<span class="chip">${esc(s.status)}</span>` : ""}<span class="chip">${esc(s.series_type)}</span>
      ${s.tmdb_id ? `<a class="chip" target="_blank" rel="noopener" href="https://www.themoviedb.org/tv/${s.tmdb_id}">TMDB</a>` : ""}
      ${s.tvdb_id ? `<a class="chip" target="_blank" rel="noopener" href="https://thetvdb.com/dereferrer/series/${s.tvdb_id}">TheTVDB</a>` : ""}</div>
    <p>${esc(s.overview)}</p>${versionBar("tv", s)}${isAdmin() ? `<p class="small muted">Folder: <code>${esc(s.path)}</code></p>` : ""}
    ${multi ? `<p class="small"><b>Showing the ${TIER_LABEL[s.tier]} version.</b> Episodes, searches and monitoring below are for this version only.</p>` : ""}
    <div class="toolbar admin-only"><button class="primary" id="as">Search all missing</button><button id="rf">Refresh episode list</button>
      ${multi ? "" : '<select id="tier" title="Move to the other library"><option value="hd">1080p library</option><option value="uhd">4K library</option></select>'}
      <select id="stype"><option value="standard">Standard</option><option value="daily">Daily</option><option value="anime">Anime</option></select>
      <label><input type="checkbox" id="mon" ${s.monitored ? "checked" : ""}> Monitored</label>
      <button id="rs">Rescan folder</button>
      <button class="danger" id="del">${multi ? `Remove ${TIER_LABEL[s.tier]} version` : "Delete"}</button></div></div></div>
    ${Object.keys(seasons).sort((a, b) => b - a).map((sn) => {
      const eps = seasons[sn]; const have = eps.filter((e) => e.file_path).length;
      return `<details class="season" ${sn == Math.max(...Object.keys(seasons)) ? "open" : ""}><summary>${sn == 0 ? "Specials" : "Season " + sn} <span class="muted small">${have}/${eps.length}</span>
        <button class="small admin-only" onclick="event.preventDefault();seasonSearch(${id},${sn})">Search season</button>
        <button class="small admin-only" onclick="event.preventDefault();interactive('tv',${id},'/series/${id}/search?season=${sn}&interactive=true')">Interactive</button>
        <button class="small admin-only" onclick="event.preventDefault();monSeason(${id},${sn},true)">Monitor all</button>
        <button class="small admin-only" onclick="event.preventDefault();monSeason(${id},${sn},false)">Unmonitor all</button></summary>
        <div class="table-wrap"><table><tr><th></th><th>#</th><th>Title</th><th>Air date</th><th>File</th><th></th></tr>
        ${eps.map((e) => `<tr><td><input type="checkbox" ${e.monitored ? "checked" : ""} ${isAdmin() ? "" : "disabled"} onchange="monEp(${e.id}, this.checked)"></td><td>${e.episode}</td><td>${esc(e.title || "TBA")}</td><td class="small">${esc(e.air_date || "")}</td>
          <td>${e.download ? `<span class="chip warn">${esc(e.download.status)} ${Math.round(e.download.progress)}%</span>` : e.file_path ? `<span class="chip ok" title="${isAdmin() ? esc(e.file_path) : ""}">${esc(e.file_quality)}</span>` : e.air_date && e.air_date <= today ? (e.monitored ? '<span class="chip bad">Missing</span>' : '<span class="chip">Skipped</span>') : '<span class="chip">Unaired</span>'}</td>
          <td class="admin-only"><button class="small" onclick="epSearch(${id},${e.id},this)">Search</button> <button class="small" onclick="interactive('tv',${id},'/series/${id}/search?episode_id=${e.id}&interactive=true')">…</button></td></tr>`).join("")}</table></div></details>`;
    }).join("")}`;
  $("#stype").value = s.series_type || "standard";
  if ($("#tier")) {
    $("#tier").value = s.tier;
    $("#tier").onchange = () => guard(async () => { await api("/series/" + id, { method: "PUT", body: { tier: $("#tier").value } }); toast("Library changed"); route(); });
  }
  $("#stype").onchange = () => guard(() => api("/series/" + id, { method: "PUT", body: { series_type: $("#stype").value } }));
  $("#mon").onchange = () => guard(() => api("/series/" + id, { method: "PUT", body: { monitored: $("#mon").checked } }));
  $("#as").onclick = () => guard(async () => { $("#as").disabled = true; const r = await api(`/series/${id}/search`, { method: "POST" }); toast(`${r.filter((x) => x.accepted).length} acceptable releases found`); route(); });
  $("#rs").onclick = () => rescan(`/series/${id}/rescan`);
  $("#rf").onclick = () => guard(async () => { await api(`/series/${id}/refresh`, { method: "POST" }); toast("Refreshed"); route(); });
  $("#del").onclick = () => guard(() => deleteVersion("tv", s));
}
async function rescan(path) {
  const rename = confirm("Also rename the files to your TRaSH naming scheme?\n\nOK = rescan and rename, Cancel = rescan only");
  await guard(async () => { const r = await api(`${path}?rename=${rename}`, { method: "POST" }); toast(`Found ${r.files} file(s)`); route(); });
}
async function seasonSearch(id, sn) { toast("Searching season " + sn + "…"); await guard(async () => { const r = await api(`/series/${id}/search?season=${sn}`, { method: "POST" }); toast(`${r.filter((x) => x.accepted).length} acceptable releases`); route(); }); }
async function epSearch(id, eid, btn) { btn.disabled = true; await guard(async () => { const r = await api(`/series/${id}/search?episode_id=${eid}`, { method: "POST" }); toast(r.some((x) => x.accepted) ? "Release grabbed" : "Nothing acceptable found"); route(); }); }
async function monEp(eid, on) { await guard(() => api("/episodes/monitor", { method: "PUT", body: { ids: [eid], monitored: on } })); }
async function monSeason(id, sn, on) {
  const s = await api("/series/" + id);
  await guard(() => api("/episodes/monitor", { method: "PUT", body: { ids: s.episodes.filter((e) => e.season == sn).map((e) => e.id), monitored: on } }));
  route();
}

// ---------------------------------------------------------------- add ---
async function renderAdd() {
  view.innerHTML = `<h2>Add to library</h2>
  <div class="toolbar"><select id="kind"><option value="movie">Movie (TMDB)</option><option value="series">TV series</option><option value="collection">Movie collection / universe (TMDB)</option></select>
  <input id="term" class="grow" placeholder="Title, optionally with year — or tmdb:603 / tvdb:81189 (TV: tmdb:1399)" autofocus><button class="primary" id="go">Search</button></div>
  <div id="results" class="panel" style="padding:0"></div>`;
  const go = () => guard(async () => {
    const kind = $("#kind").value, term = $("#term").value.trim();
    if (!term) return;
    $("#results").innerHTML = '<p class="muted" style="padding:12px">Searching…</p>';
    const res = await api(`/lookup/${kind}?term=${encodeURIComponent(term)}`);
    window._lookup = res;
    if (kind === "collection") {
      $("#results").innerHTML = res.map((r) => `<div class="result-row">${r.poster ? `<img src="${esc(r.poster)}" alt="">` : ""}
        <div class="grow"><b>${esc(r.name)}</b> <span class="chip">${r.source === "keyword" ? "Universe (keyword)" : "Collection"}</span>${r.saved ? ' <span class="chip ok">Added</span>' : ""}<p>${esc(r.overview)}</p></div>
        <div class="add-ctrls"><button class="primary" onclick="openCollection('${r.source}', ${r.id})">View &amp; add</button></div></div>`).join("") || '<p class="muted" style="padding:12px">No collections found. Tip: universes like the MCU are TMDB keywords, so try "marvel cinematic universe".</p>';
      return;
    }
    $("#results").innerHTML = res.map((r, i) => `<div class="result-row">${r.poster ? `<img src="${esc(r.poster)}" alt="">` : ""}
      <div class="grow"><b>${esc(r.title)}</b> <span class="muted">${r.year || ""}</span>${r.status ? ` <span class="chip">${esc(r.status)}</span>` : ""}<p>${esc(r.overview)}</p>
        ${r.collection ? `<button class="small" onclick="openCollection('collection', ${r.collection.id})">Part of ${esc(r.collection.name)} — add whole collection…</button>` : ""}</div>
      <div class="add-ctrls">${r.in_library || (!canUhd() && (r.library_tiers || []).includes("hd")) ? '<span class="chip ok">In library</span>' : `
        ${(r.library_tiers || []).length ? `<span class="chip ok">In library: ${r.library_tiers.map((x) => TIER_LABEL[x]).join(", ")}</span>` : ""}
        <select id="t${i}">${tierOptions(true, r.library_tiers || [])}</select>
        ${kind === "series" ? `<select id="m${i}"><option value="all">All episodes</option><option value="future">Future episodes</option><option value="latest">Latest season</option>${isAdmin() ? '<option value="none">None</option>' : ""}</select>` : ""}
        <label class="small"><input type="checkbox" id="s${i}" checked> Search now</label>
        <button class="primary" onclick="addItem('${kind}', ${i}, this)">${addLabel()}</button>`}</div></div>`).join("") || '<p class="muted" style="padding:12px">No results.</p>';
  });
  $("#go").onclick = go;
  $("#term").addEventListener("keydown", (e) => e.key === "Enter" && go());
}
// Admins add straight to the library; users create a request (auto-approved if an admin allowed that).
async function addOrRequest(kind, body) {
  if (!isAdmin()) {
    const r = await api("/requests", { method: "POST", body: { kind, ...body } });
    const ok = r.status === "approved" || r.status === "available";
    toast(r.duplicate ? `${r.title} has already been requested` : ok ? `Added ${r.title}` : r.status === "failed" ? `${r.title}: ${r.note}` : `Requested ${r.title} — waiting for an admin to approve`, r.status === "failed");
    refreshBadge();
    return { id: ok ? r.item_id : null, requested: !ok, title: r.title };
  }
  if (kind === "movie") return api("/movies", { method: "POST", body });
  if (kind === "tv") return api("/series", { method: "POST", body });
  return api("/collections", { method: "POST", body });
}
const addLabel = () => (isAdmin() ? "Add" : "Request");

async function addItem(kind, i, btn) {
  const r = window._lookup[i]; btn.disabled = true;
  await guard(async () => {
    btn.textContent = isAdmin() ? "Adding…" : "Requesting…";
    const res = kind === "movie"
      ? await addOrRequest("movie", { tmdb_id: r.tmdb_id, tier: $("#t" + i).value, search: $("#s" + i).checked })
      : await addOrRequest("tv", { tvdb_id: r.tvdb_id || undefined, tmdb_id: r.tmdb_id || undefined, tier: $("#t" + i).value, monitor: $("#m" + i).value, search: $("#s" + i).checked });
    if (isAdmin()) toast(`Added ${res.title}`);
    btn.textContent = res.requested ? "Requested" : "Added";
  });
}

// ----------------------------------------------------------- discover ---
const PINNED = ["netflix", "apple tv", "amazon prime video", "disney plus", "max", "hulu", "paramount plus", "peacock"];
async function renderDiscover(pid) {
  if (pid) return renderProvider(+pid);
  const { region, providers } = await api("/providers");
  const pinRank = (p) => { const i = PINNED.findIndex((n) => p.name.toLowerCase().replace("+", " plus").startsWith(n)); return i < 0 ? 99 : i; };
  const sorted = [...providers].sort((a, b) => pinRank(a) - pinRank(b) || a.priority - b.priority);
  const tile = (p) => `<a class="provider" href="#/discover/${p.id}">${p.logo ? `<img src="${esc(p.logo)}" alt="">` : ""}<span>${esc(p.name)}</span></a>`;
  view.innerHTML = `<div class="toolbar"><h2 class="grow">Streaming services <span class="muted small">available in ${esc(region)}</span></h2>
    <input id="pf" placeholder="Find a service…"></div>
    <div class="providers" id="pg">${sorted.slice(0, 24).map(tile).join("")}</div>
    <div class="toolbar" style="margin-top:14px"><button id="pmore">Show all ${providers.length} services</button>
    <span class="small muted">Change the country under Settings → Metadata. Data from TMDB / JustWatch.</span></div>`;
  $("#pmore").onclick = () => { $("#pg").innerHTML = sorted.map(tile).join(""); $("#pmore").remove(); };
  $("#pf").addEventListener("input", () => { const f = $("#pf").value.toLowerCase(); $("#pg").innerHTML = sorted.filter((p) => p.name.toLowerCase().includes(f)).map(tile).join(""); });
}
const disc = { kind: "movie", sort: "popular", originals: false, page: 1 };
async function renderProvider(pid) {
  const { providers } = await api("/providers");
  const prov = providers.find((p) => p.id === pid) || { name: "Service", kinds: ["movie", "tv"] };
  if (!prov.kinds.includes(disc.kind)) disc.kind = prov.kinds[0];
  disc.page = 1;
  view.innerHTML = `<div class="toolbar"><div class="prov-head grow">${prov.logo ? `<img src="${esc(prov.logo)}" alt="">` : ""}<h2 style="margin:0">${esc(prov.name)}</h2><span class="muted small" id="dcount"></span></div>
    <a href="#/discover"><button>All services</button></a></div>
    <div class="toolbar"><div class="tabs2" style="margin:0">${["movie", "tv"].filter((k) => prov.kinds.includes(k)).map((k) => `<button data-k="${k}" class="${disc.kind === k ? "active" : ""}">${k === "movie" ? "Movies" : "TV shows"}</button>`).join("")}</div>
      <select id="dsort"><option value="popular">Most popular</option><option value="newest">Newest</option><option value="rating">Top rated</option><option value="title">A–Z</option></select>
      ${prov.originals ? `<label id="dorigw"><input type="checkbox" id="dorig" ${disc.originals ? "checked" : ""}> ${esc(prov.name)} originals only</label>` : ""}
      <span class="grow"></span>
      <label>${addLabel()} for <select id="dtier">${tierOptions()}</select></label>
      <select id="dmon" title="TV: which episodes to monitor"><option value="all">All episodes</option><option value="future">Future episodes</option><option value="latest">Latest season</option></select>
      <label class="small"><input type="checkbox" id="dsearch" checked> Search on add</label></div>
    <div class="grid" id="dg"></div><div class="toolbar" style="justify-content:center;margin-top:16px"><button id="dmore">Load more</button></div>`;
  $("#dsort").value = disc.sort;
  const showMon = () => { $("#dmon").style.display = disc.kind === "tv" ? "" : "none"; if ($("#dorigw")) $("#dorigw").style.display = disc.kind === "tv" ? "" : "none"; };
  showMon();
  const load = async (reset) => {
    if (reset) { disc.page = 1; $("#dg").innerHTML = '<p class="muted">Loading…</p>'; }
    const d = await api(`/providers/${pid}/${disc.kind}?page=${disc.page}&sort=${disc.sort}&originals=${disc.kind === "tv" && disc.originals}`);
    window._disc = reset ? d.results : [...(window._disc || []), ...d.results];
    $("#dcount").textContent = `${d.total_results.toLocaleString()} ${disc.kind === "movie" ? "movies" : "shows"}`;
    $("#dg").innerHTML = window._disc.map((r, i) => `<div class="card" ${r.library_id ? `onclick="location.hash='#/${disc.kind === "movie" ? "movie" : "show"}/${r.library_id}'"` : ""} title="${esc(r.overview)}">${poster(r.poster, r.title)}
      <div class="meta"><div class="title" title="${esc(r.title)}">${esc(r.title)}</div>
      <div class="chips"><span class="chip">${r.year || "TBA"}</span>${r.rating ? `<span class="chip">★ ${r.rating.toFixed(1)}</span>` : ""}</div>
      ${r.library_id ? '<span class="chip ok">In library</span>' : `<button class="small primary add-btn" onclick="event.stopPropagation();discAdd(${i}, this)">+ ${addLabel()}</button>`}</div></div>`).join("") || '<p class="muted">Nothing found.</p>';
    $("#dmore").style.display = d.page < d.total_pages ? "" : "none";
  };
  view.querySelectorAll(".tabs2 button").forEach((b) => (b.onclick = () => { disc.kind = b.dataset.k; view.querySelectorAll(".tabs2 button").forEach((x) => x.classList.toggle("active", x === b)); showMon(); guard(() => load(true)); }));
  $("#dsort").onchange = () => { disc.sort = $("#dsort").value; guard(() => load(true)); };
  if ($("#dorig")) $("#dorig").onchange = () => { disc.originals = $("#dorig").checked; guard(() => load(true)); };
  $("#dmore").onclick = () => { disc.page++; guard(() => load(false)); };
  await load(true);
}
async function discAdd(i, btn) {
  const r = window._disc[i];
  btn.disabled = true; btn.textContent = "Adding…";
  await guard(async () => {
    const tier = $("#dtier").value, search = $("#dsearch").checked;
    const item = disc.kind === "movie"
      ? await addOrRequest("movie", { tmdb_id: r.tmdb_id, tier, search })
      : await addOrRequest("tv", { tmdb_id: r.tmdb_id, tier, monitor: $("#dmon").value, search });
    r.library_id = item.id;
    btn.outerHTML = item.requested ? '<span class="chip warn">Requested</span>' : '<span class="chip ok">Added</span>';
    if (isAdmin()) toast(`Added ${item.title}`);
  });
  if (btn.isConnected) { btn.disabled = false; btn.textContent = "+ " + addLabel(); }
}

// -------------------------------------------------------- collections ---
async function openCollection(source, id) {
  modal('<p class="muted">Loading collection…</p>');
  const c = await guard(() => api(`/collection/${source}/${id}`));
  if (!c) return closeModal();
  const today = new Date().toISOString().slice(0, 10);
  const saved = c.saved;
  const excluded = new Set(saved ? JSON.parse(saved.excluded || "[]") : []);
  $("#modal .modal-body").innerHTML = `<div class="toolbar"><h2 class="grow">${esc(c.name)} <span class="muted small">${c.parts.length} movies</span></h2><button onclick="closeModal()">Close</button></div>
    ${c.overview ? `<p class="muted">${esc(c.overview)}</p>` : ""}
    <div class="panel"><div class="toolbar">
      <label>Library <select id="ctier">${tierOptions(false)}</select></label>
      <label><input type="checkbox" id="cmon" ${!saved || saved.monitored ? "checked" : ""}> Add future movies in this collection automatically</label>
      <label><input type="checkbox" id="csearch" checked> Search now</label>
      <span class="grow"></span><button class="small" id="call">Select all</button><button class="small" id="cnone">Select none</button>
      <button class="primary" id="cadd">${addLabel()} selected</button></div></div>
    <div class="table-wrap"><table><tr><th></th><th></th><th>Movie</th><th>Released</th><th></th></tr>
    ${c.parts.map((p) => `<tr><td><input type="checkbox" class="cpick" value="${p.tmdb_id}" ${p.in_library ? "checked disabled" : excluded.has(p.tmdb_id) ? "" : "checked"}></td>
      <td>${p.poster ? `<img src="${esc(p.poster)}" alt="" style="width:40px;border-radius:3px">` : ""}</td>
      <td><b>${esc(p.title)}</b> <span class="muted">${p.year || ""}</span></td>
      <td class="small">${esc(p.release_date || "TBA")}${!p.release_date || p.release_date > today ? ' <span class="chip warn">upcoming</span>' : ""}</td>
      <td>${p.in_library ? `${(p.tiers || [p.tier]).map(tierChip).join("")} <span class="chip ${p.has_file ? "ok" : ""}">${p.has_file ? "Downloaded" : "In library"}</span>` : ""}</td></tr>`).join("")}</table></div>
    <p class="small muted">Unticked movies are remembered and won't be added automatically later. Upcoming movies are added now and searched for once they're released.</p>`;
  if (saved && (saved.tier === "hd" || canUhd())) $("#ctier").value = saved.tier;
  // a movie already in the library can still get its other version when the collection is set to both
  if (canUhd()) $("#ctier").addEventListener("change", () => document.querySelectorAll(".cpick").forEach((b) => {
    const p = c.parts.find((x) => x.tmdb_id === +b.value);
    if (p && p.in_library) { const missing = $("#ctier").value === "both" ? (p.tiers || []).length < 2 : !(p.tiers || []).includes($("#ctier").value); b.disabled = !missing; b.checked = missing; }
  }));
  const boxes = () => [...document.querySelectorAll(".cpick:not(:disabled)")];
  $("#call").onclick = () => boxes().forEach((b) => (b.checked = true));
  $("#cnone").onclick = () => boxes().forEach((b) => (b.checked = false));
  $("#cadd").onclick = () => guard(async () => {
    $("#cadd").disabled = true; $("#cadd").textContent = isAdmin() ? "Adding…" : "Requesting…";
    const ids = [...document.querySelectorAll(".cpick:checked")].map((b) => +b.value);
    const r = await addOrRequest("collection", { source, tmdb_id: id, tier: $("#ctier").value, tmdb_ids: ids, monitor: $("#cmon").checked, search: $("#csearch").checked });
    if (isAdmin()) toast(`${r.name}: added ${r.added} movie(s)`);
    closeModal(); if (location.hash.startsWith("#/collections")) route();
  });
}
async function renderCollections() {
  const rows = await api("/collections");
  view.innerHTML = `<div class="toolbar"><h2 class="grow">Collections <span class="muted small">(${rows.length})</span></h2>
    <a href="#/add"><button class="primary">${addLabel()} a collection</button></a></div>
    ${rows.length ? `<div class="table-wrap"><table><tr><th></th><th>Collection</th><th>Have</th><th>Library</th><th>Auto-add new</th><th></th></tr>
    ${rows.map((c) => { const have = new Set(c.movies.filter((m) => m.file_path).map((m) => m.tmdb_id)).size; c.movies = groupVersions(c.movies, (m) => m.tmdb_id).map((vs) => vs[0]); return `<tr>
      <td>${c.poster ? `<img src="${esc(c.poster)}" alt="" style="width:46px;border-radius:4px">` : ""}</td>
      <td><b>${esc(c.name)}</b> <span class="chip">${c.source === "keyword" ? "Universe" : "Collection"}</span>
        <div class="small muted">${c.movies.map((m) => `<a href="#/movie/${m.id}" style="color:inherit">${esc(m.title)}</a>`).join(" · ")}</div></td>
      <td><span class="chip ${have && have >= c.total ? "ok" : ""}">${have}/${c.total}</span></td>
      <td>${isAdmin() ? "" : tierChip(c.tier)}<select class="admin-only" onchange="editColl(${c.id}, {tier: this.value})">${["hd", "uhd", "both"].map((v) => `<option value="${v}" ${c.tier === v ? "selected" : ""}>${TIER_LABEL[v]}</option>`).join("")}</select></td>
      <td><input type="checkbox" ${c.monitored ? "checked" : ""} ${isAdmin() ? "" : "disabled"} onchange="editColl(${c.id}, {monitored: this.checked})"></td>
      <td><button class="small" onclick="openCollection('${c.source}', ${c.tmdb_id})">View</button>
        <button class="small admin-only" onclick="refreshColl(${c.id})">Check for new</button>
        <button class="small danger admin-only" onclick="delColl(${c.id})">Remove</button></td></tr>`; }).join("")}</table></div>
    <p class="small muted">"Library" decides where newly found movies go; movies already added stay where they are. Removing a collection keeps its movies.</p>`
    : '<p class="muted">No collections yet. On the Add page, search for a movie that is part of a collection, or switch the search to "Movie collection / universe".</p>'}`;
}
async function editColl(id, body) { await guard(() => api("/collections/" + id, { method: "PUT", body })); }
async function refreshColl(id) { await guard(async () => { const r = await api(`/collections/${id}/refresh`, { method: "POST" }); toast(r.added ? `Added ${r.added} new movie(s)` : "Nothing new"); route(); }); }
async function delColl(id) { if (confirm("Stop tracking this collection? Its movies stay in your library.")) await guard(async () => { await api("/collections/" + id, { method: "DELETE" }); route(); }); }

// ----------------------------------------------------------- activity ---
async function renderActivity(sub = "queue") {
  view.innerHTML = `<div class="tabs2">${["queue", "history", "events", "blocklist"].map((t) => `<button class="${t === sub ? "active" : ""}" onclick="location.hash='#/activity/${t}'">${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}
    <span class="grow"></span><button onclick="runTask('rss')">Run RSS sync</button><button onclick="runTask('missing')">Search missing</button><button onclick="runTask('downloads')">Check downloads</button></div><div id="act"></div>`;
  const el = $("#act");
  if (sub === "queue" || sub === "history") {
    const rows = (await api("/queue")).filter((d) => (sub === "queue") === ["queued", "downloading", "importing", "import_failed"].includes(d.status));
    el.innerHTML = rows.length ? `<div class="table-wrap"><table><tr><th>Item</th><th>Release</th><th>Quality</th><th>Score</th><th>Status</th><th></th></tr>
      ${rows.map((d) => `<tr><td>${esc(d.movie_title || d.series_title || "")}</td><td>${esc(d.release_title)}<div class="small muted">${esc(d.indexer)} · ${size(d.size)} · ${esc(d.added)}</div>${d.message ? `<div class="rej">${esc(d.message)}</div>` : ""}</td>
      <td>${esc(d.quality)}</td><td>${d.score}</td><td>${statusChip(d)}${d.status === "downloading" ? `<div class="progress"><div style="width:${d.progress}%"></div></div>` : ""}</td>
      <td>${d.status === "import_failed" ? `<button class="small" onclick="retryImport(${d.id})">Retry import</button> ` : ""}${sub === "queue" ? `<button class="small danger" onclick="rmDl(${d.id})">Remove</button>` : ""}</td></tr>`).join("")}</table></div>` : '<p class="muted">Empty.</p>';
    if (sub === "queue") setTimeout(() => { if (location.hash.startsWith("#/activity/queue") || location.hash === "#/activity") renderActivity("queue"); }, 10000);
  } else if (sub === "events") {
    const rows = await api("/events");
    el.innerHTML = `<div class="table-wrap"><table><tr><th>Time</th><th>Level</th><th>Message</th></tr>${rows.map((e) => `<tr><td class="small muted">${esc(e.ts)}</td><td><span class="chip ${e.level === "error" ? "bad" : e.level === "warn" ? "warn" : ""}">${esc(e.level)}</span></td><td>${esc(e.message)}</td></tr>`).join("")}</table></div>`;
  } else {
    const rows = await api("/blocklist");
    el.innerHTML = rows.length ? `<div class="table-wrap"><table><tr><th>Release</th><th>Reason</th><th>Added</th><th></th></tr>${rows.map((b) => `<tr><td>${esc(b.release_title)}</td><td>${esc(b.reason)}</td><td class="small muted">${esc(b.added)}</td><td><button class="small" onclick="unblock(${b.id})">Remove</button></td></tr>`).join("")}</table></div>` : '<p class="muted">Empty.</p>';
  }
}
routes.activity = (sub) => renderActivity(sub || "queue");
async function runTask(n) { await guard(async () => { await api("/tasks/" + n, { method: "POST" }); toast("Started " + n); }); }
async function retryImport(id) { await guard(async () => { await api(`/queue/${id}/retry-import`, { method: "POST" }); toast("Imported"); route(); }); }
async function rmDl(id) { if (!confirm("Remove download?")) return; const bl = confirm("Blocklist this release and search for another?"); await guard(async () => { await api(`/queue/${id}?blocklist=${bl}`, { method: "DELETE" }); route(); }); }
async function unblock(id) { await guard(async () => { await api("/blocklist/" + id, { method: "DELETE" }); route(); }); }

// ----------------------------------------------------------- settings ---
const SECTIONS = {
  general: { title: "Metadata", fields: [
    ["tmdb_api_key", "TMDB API key (required, free)", "password", "themoviedb.org → Settings → API. v3 key or v4 read token. Used for both movies and TV."],
    ["metadata_language", "Metadata language", "text", "Two-letter code, e.g. en"],
    ["streaming_region", "Streaming region", "text", "Country for the Streaming tab, e.g. US, GB, CA, AU, DE"],
    ["streaming_hide_ads", "Hide \"with Ads\" plans", "checkbox", "Leaves the ad plans of paid services (e.g. \"Netflix Standard with Ads\", \"Amazon Prime Video with Ads\") off the Streaming tab. Netflix, Prime Video and free services like Tubi stay."],
  ] },
  downloads: { title: "NZBGet", fields: [
    ["nzbget_url", "NZBGet URL", "text", "e.g. http://192.168.1.10:6789"],
    ["nzbget_username", "Username", "text"], ["nzbget_password", "Password", "password"],
    ["nzbget_category_movies", "Movie category", "text"], ["nzbget_category_tv", "TV category", "text"],
    ["remote_path_from", "Remote path (NZBGet)", "text", "If NZBGet sees downloads at a different path, e.g. /downloads/completed"],
    ["remote_path_to", "Local path (Omniarr)", "folder", "…and Omniarr sees them at, e.g. /data/downloads/completed"],
    ["import_mode", "Import mode", ["move", "hardlink", "copy"], "Hardlink needs downloads and media on the same share (e.g. /mnt/user/data)."],
  ] },
  roots: { title: "Root folders", fields: [
    ["root_movies_hd", "Movies — 1080p and lower", "folder"], ["root_movies_uhd", "Movies — 4K", "folder", "Pick the same folder as 1080p to keep both versions of a movie side by side in one folder."],
    ["root_tv_hd", "TV — 1080p and lower", "folder"], ["root_tv_uhd", "TV — 4K", "folder", "Same folder as 1080p = both versions of a show in one folder."],
  ] },
  schedule: { title: "Schedule", fields: [
    ["rss_interval_minutes", "RSS sync (minutes)", "number"], ["search_interval_minutes", "Backlog search (minutes)", "number", "Searches up to 10 missing items per run, each at most once a day."],
    ["series_refresh_hours", "Refresh TV info (hours)", "number"], ["trash_auto_sync_hours", "Re-sync TRaSH Guides (hours)", "number", "0 = never automatically"],
    ["upgrades_enabled", "Upgrade until cutoff", "checkbox"],
  ] },
};
async function renderSettings(sub = "general") {
  const s = await api("/settings");
  const tabs = { general: "Metadata", downloads: "NZBGet", indexers: "Indexers", roots: "Root folders", profiles: "Profiles (TRaSH)", limits: "Quality limits", users: "Users", naming: "Naming (TRaSH)", schedule: "Schedule" };
  view.innerHTML = `<h2>Settings</h2><div class="tabs2">${Object.entries(tabs).map(([k, v]) => `<button class="${k === sub ? "active" : ""}" onclick="location.hash='#/settings/${k}'">${v}</button>`).join("")}</div><div id="set" class="panel"></div>`;
  const el = $("#set");
  if (SECTIONS[sub]) {
    el.innerHTML = `<div class="form">${SECTIONS[sub].fields.map(([k, label, type, help]) => {
      let input;
      if (Array.isArray(type)) input = `<select data-k="${k}">${type.map((o) => `<option ${s[k] === o ? "selected" : ""}>${o}</option>`).join("")}</select>`;
      else if (type === "checkbox") input = `<input type="checkbox" data-k="${k}" ${s[k] ? "checked" : ""}>`;
      else if (type === "folder") input = `<div class="folder-field"><input type="text" data-k="${k}" value="${esc(s[k])}" autocomplete="off"><button type="button" onclick="pickFolder(this.previousElementSibling)">Browse…</button></div>`;
      else input = `<input type="${type}" data-k="${k}" value="${esc(s[k])}" autocomplete="off">`;
      return `<label>${label}</label>${input}${help ? `<div class="help">${help}</div>` : ""}`;
    }).join("")}<span></span><div class="toolbar"><button class="primary" id="save">Save</button>${sub === "downloads" ? '<button id="test">Test NZBGet</button>' : ""}</div></div>`;
    $("#save").onclick = () => guard(async () => { await api("/settings", { method: "PUT", body: collect(el) }); toast("Saved"); checkBanner(); });
    if ($("#test")) $("#test").onclick = () => guard(async () => { await api("/settings", { method: "PUT", body: collect(el) }); const r = await api("/nzbget/test", { method: "POST" }); toast(r.message, !r.ok); });
  } else if (sub === "indexers") await renderIndexers(el);
  else if (sub === "profiles") await renderProfiles(el, s);
  else if (sub === "limits") await renderLimits(el);
  else if (sub === "users") await renderUsers(el);
  else if (sub === "naming") await renderNaming(el, s);
}
// Folder picker: browse the folders Omniarr can see (/data = the Data share from the Unraid template)
async function pickFolder(input) {
  const join = (a, b) => (a.endsWith("/") ? a : a + "/") + b;
  modal('<p class="muted">Loading folders…</p>');
  const draw = async (path) => {
    const d = await guard(() => api("/fs/browse?path=" + encodeURIComponent(path)));
    if (!d) return;
    const body = $("#modal .modal-body");
    body.innerHTML = `<div class="toolbar"><h2 class="grow">Choose a folder</h2><button onclick="closeModal()">Cancel</button></div>
      <div class="folder-path"><code>${esc(d.path)}</code></div>
      <div class="folder-list">${d.parent !== null ? `<button class="folder up" data-p="${esc(d.parent)}">↑ Up one level</button>` : ""}
        ${d.folders.map((f) => `<button class="folder" data-p="${esc(join(d.path, f))}"><span class="ficon"></span>${esc(f)}</button>`).join("") || '<p class="muted small" style="padding:8px">No folders inside this one.</p>'}</div>
      <div class="toolbar" style="margin-top:12px"><button id="fnew">New folder…</button><span class="grow"></span><button class="primary" id="fuse">Use this folder</button></div>
      <p class="small muted">These are the paths Omniarr sees. <code>/data</code> is the Data share set in the Unraid template, for example <code>/mnt/user/media</code>.</p>`;
    body.querySelectorAll(".folder").forEach((b) => (b.onclick = () => draw(b.dataset.p)));
    $("#fuse").onclick = () => { input.value = d.path; input.dispatchEvent(new Event("input")); closeModal(); toast("Folder chosen — click Save to keep it"); };
    $("#fnew").onclick = () => guard(async () => {
      const name = prompt(`New folder inside ${d.path}:`);
      if (!name) return;
      const r = await api("/fs/mkdir", { method: "POST", body: { path: d.path, name } });
      await draw(r.path);
    });
  };
  await draw(input.value || "");
}
function collect(el) {
  const out = {};
  el.querySelectorAll("[data-k]").forEach((i) => { out[i.dataset.k] = i.type === "checkbox" ? i.checked : i.type === "number" ? Number(i.value) : i.value; });
  return out;
}
async function renderIndexers(el) {
  const rows = await api("/indexers");
  el.innerHTML = `<p class="muted">Any Newznab-compatible Usenet indexer. Movies use category 2000 and TV 5000 by default.</p>
  ${rows.length ? `<div class="table-wrap"><table><tr><th>Name</th><th>URL</th><th>Categories (movie / tv)</th><th>Priority</th><th>Enabled</th><th></th></tr>
  ${rows.map((r) => `<tr><td>${esc(r.name)}</td><td class="small">${esc(r.url)}</td><td class="small">${esc(r.movie_categories)} / ${esc(r.tv_categories)}</td><td>${r.priority}</td>
    <td><input type="checkbox" ${r.enabled ? "checked" : ""} onchange="toggleIx(${r.id}, this.checked)"></td>
    <td><button class="small" onclick="testIx(${r.id})">Test</button> <button class="small danger" onclick="delIx(${r.id})">Delete</button></td></tr>`).join("")}</table></div>` : ""}
  <h3>Add indexer</h3><div class="form" id="ixf">
    <label>Name</label><input data-k="name" placeholder="NZBGeek">
    <label>URL</label><input data-k="url" placeholder="https://api.nzbgeek.info">
    <label>API key</label><input data-k="apikey" type="password" autocomplete="off">
    <label>Movie categories</label><input data-k="movie_categories" value="2000">
    <label>TV categories</label><input data-k="tv_categories" value="5000">
    <label>Priority</label><input data-k="priority" type="number" value="25"><div class="help">Lower = preferred.</div>
    <span></span><div><button class="primary" id="addix">Add</button></div></div>`;
  $("#addix").onclick = () => guard(async () => { const b = collect($("#ixf")); if (!b.name || !b.url || !b.apikey) throw new Error("Name, URL and API key are required"); const r = await api("/indexers", { method: "POST", body: b }); const t = await api(`/indexers/${r.id}/test`, { method: "POST" }); toast(t.ok ? `Added — ${t.message}` : `Added, but test failed: ${t.message}`, !t.ok); route(); });
}
async function toggleIx(id, on) { await guard(() => api("/indexers/" + id, { method: "PUT", body: { enabled: on } })); }
async function testIx(id) { await guard(async () => { const r = await api(`/indexers/${id}/test`, { method: "POST" }); toast(r.ok ? "OK — " + r.message : r.message, !r.ok); }); }
async function delIx(id) { if (confirm("Delete indexer?")) await guard(async () => { await api("/indexers/" + id, { method: "DELETE" }); route(); }); }

async function renderProfiles(el, s) {
  const tp = await api("/trash/profiles");
  const st = await api("/status");
  const opts = (list, cur) => list.map((p) => `<option value="${esc(p.key)}" ${p.key === cur ? "selected" : ""}>${esc(p.name)} (${esc(p.key)})</option>`).join("");
  el.innerHTML = `<p>Quality profiles, custom formats, scores and size limits are imported straight from <a href="https://trash-guides.info" target="_blank" rel="noopener">TRaSH Guides</a>.
    Last sync: <b>${esc(st.trash_synced || "never")}</b> <button id="sync" class="primary">Sync TRaSH Guides now</button></p>
  ${tp.loaded ? `<div class="form">
    <label>Movies — 1080p library</label><select data-k="profile_movies_hd">${opts(tp.movie, s.profile_movies_hd)}</select>
    <label>Movies — 4K library</label><select data-k="profile_movies_uhd">${opts(tp.movie, s.profile_movies_uhd)}</select>
    <label>TV — 1080p library</label><select data-k="profile_tv_hd">${opts(tp.tv, s.profile_tv_hd)}</select>
    <label>TV — 4K library</label><select data-k="profile_tv_uhd">${opts(tp.tv, s.profile_tv_uhd)}</select>
    <label>Score set</label><input data-k="trash_score_set" value="${esc(s.trash_score_set)}"><div class="help">TRaSH score column: default, sqp-1-1080p, french, german, anime…</div>
    <span></span><div><button class="primary" id="save">Save</button></div></div>
    <h3>Preview</h3><div class="toolbar"><select id="pv"><option value="movie/hd">Movies 1080p</option><option value="movie/uhd">Movies 4K</option><option value="tv/hd">TV 1080p</option><option value="tv/uhd">TV 4K</option></select></div><div id="pvo"></div>
    <h3>Test a release name</h3><div class="toolbar"><input id="rt" class="grow" placeholder="Movie.Name.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR10.H.265-FLUX"><button id="rtb">Score it</button></div><div id="rto"></div>` : '<p class="muted">Sync first to choose profiles.</p>'}`;
  $("#sync").onclick = () => guard(async () => { $("#sync").disabled = true; $("#sync").textContent = "Syncing…"; const r = await api("/trash/sync", { method: "POST" }); toast(`Synced ${r.files} files`); checkBanner(); route(); });
  if (!tp.loaded) return;
  $("#save").onclick = () => guard(async () => { await api("/settings", { method: "PUT", body: collect(el) }); toast("Saved"); preview(); });
  const preview = () => guard(async () => {
    const p = await api("/trash/profile/" + $("#pv").value);
    $("#pvo").innerHTML = `<p><b>${esc(p.name)}</b> — cutoff <b>${esc(p.cutoff)}</b>, min score ${p.min_format_score}, upgrade until score ${p.cutoff_format_score}</p>
      <div class="chips" style="margin-bottom:10px">${p.qualities.filter((q) => q.allowed).map((q) => `<span class="chip ok">${esc(q.name)}</span>`).join("")}</div>
      <div class="chips">${p.formats.map((f) => `<span class="chip ${f.score < 0 ? "bad" : ""}">${esc(f.name)}: ${f.score}</span>`).join("")}</div>`;
  });
  $("#pv").onchange = preview; preview();
  $("#rtb").onclick = () => guard(async () => {
    const [kind, tier] = $("#pv").value.split("/");
    const r = await api(`/parse?kind=${kind}&tier=${tier}&title=${encodeURIComponent($("#rt").value)}`);
    $("#rto").innerHTML = `<p>${esc(r.profile)}: <b>${esc(r.quality)}</b> ${r.allowed ? '<span class="chip ok">allowed</span>' : '<span class="chip bad">not allowed</span>'} score <b>${r.score}</b> — ${r.custom_formats.map(esc).join(", ") || "no custom formats"}
      <br><span class="small muted">group ${esc(r.parsed.group)} · year ${esc(r.parsed.year)} · S${esc(r.parsed.season)} E${esc(r.parsed.episodes.join(","))}</span></p>`;
  });
}

// ------------------------------------------------------- quality limits ---
const LIBS = { movies_hd: "Movies — 1080p library", movies_uhd: "Movies — 4K library", tv_hd: "TV — 1080p library", tv_uhd: "TV — 4K library" };
async function renderLimits(el) {
  const all = await api("/quality-limits");
  el.innerHTML = `<p class="muted">Fine-tune each library's TRaSH profile. Omniarr always grabs the best <b>allowed</b> quality that's available right now
    (e.g. a WEB-DL), then keeps upgrading until it reaches the <b>Upgrade until</b> quality (e.g. Bluray-1080p).
    Qualities are listed best first; untick anything you never want, which sets your maximum and minimum.</p>
    <div class="tabs2" id="libtabs">${Object.entries(LIBS).map(([k, v], i) => `<button data-lib="${k}" class="${i ? "" : "active"}">${v}</button>`).join("")}</div><div id="lib"></div>`;
  const draw = (key) => {
    el.querySelectorAll("#libtabs button").forEach((b) => b.classList.toggle("active", b.dataset.lib === key));
    const L = all[key];
    if (L.error) { $("#lib").innerHTML = `<p class="rej">${esc(L.error)}</p>`; return; }
    const ov = L.override, eff = L.effective;
    $("#lib").innerHTML = `<p>Based on TRaSH profile <b>${esc(L.profile)}</b> (TRaSH default: upgrade until <b>${esc(L.trash.cutoff)}</b>, score cutoff ${L.trash.cutoff_score}).
      ${Object.keys(ov).length ? '<span class="chip warn">customised</span>' : '<span class="chip">TRaSH defaults</span>'}</p>
    <div class="form">
      <label>Allowed qualities</label><div id="ql">${L.qualities.map((q) => `<div><label style="color:var(--text)"><input type="checkbox" value="${esc(q.name)}" ${q.allowed ? "checked" : ""}> ${esc(q.name)}</label>
        ${q.items.length ? `<span class="small muted">(${q.items.map(esc).join(", ")})</span>` : ""}${q.trash_allowed ? ' <span class="chip">TRaSH</span>' : ""}</div>`).join("")}</div>
      <label>Upgrade until</label><select id="cut"></select>
      <div class="help">Once a file reaches this quality, quality upgrades stop.</div>
      <label>Upgrades</label><label style="color:var(--text)"><input type="checkbox" id="upg" ${eff.upgrades ? "checked" : ""}> Replace files with better releases</label>
      <label>Custom-format score cutoff</label><input type="number" id="cs" value="${ov.cutoff_score ?? ""}" placeholder="${L.trash.cutoff_score} (TRaSH default)">
      <div class="help">After reaching the quality above, keep upgrading to releases with a higher TRaSH score until this score. Set <b>0</b> to stop as soon as the quality is reached.</div>
      <span></span><div class="toolbar"><button class="primary" id="qs">Save</button><button id="qr">Reset to TRaSH defaults</button></div></div>`;
    const fillCut = () => {
      const allowed = [...$("#ql").querySelectorAll("input:checked")].map((i) => i.value);
      const cur = $("#cut").value || eff.cutoff;
      $("#cut").innerHTML = allowed.map((n) => `<option ${n === cur ? "selected" : ""}>${esc(n)}</option>`).join("");
    };
    $("#ql").addEventListener("change", fillCut); fillCut();
    $("#qs").onclick = () => guard(async () => {
      const allowed = [...$("#ql").querySelectorAll("input:checked")].map((i) => i.value);
      all[key] = await api("/quality-limits/" + key, { method: "PUT", body: { allowed, cutoff: $("#cut").value, upgrades: $("#upg").checked, cutoff_score: $("#cs").value } });
      toast("Saved"); draw(key);
    });
    $("#qr").onclick = () => guard(async () => { all[key] = await api("/quality-limits/" + key, { method: "PUT", body: { reset: true } }); toast("Reset"); draw(key); });
  };
  el.querySelectorAll("#libtabs button").forEach((b) => (b.onclick = () => draw(b.dataset.lib)));
  draw("movies_hd");
}

// ------------------------------------------------------- library import ---
async function renderImport() {
  view.innerHTML = `<h2>Import existing media</h2>
  <p class="muted">Scans your 1080p and 4K root folders for movies or shows that aren't in Omniarr yet, matches them on TMDB
    (IDs like <code>{tmdb-603}</code>, <code>{tvdb-81189}</code> or <code>[imdbid-tt0903747]</code> in folder names are used first), and adds them with the files already there.
    Files you already have are scored with TRaSH, so upgrades work from day one.</p>
  <div class="toolbar"><select id="ik"><option value="movie">Movies</option><option value="tv">TV shows</option></select><button class="primary" id="scan">Scan root folders</button><span class="grow"></span><span id="ist" class="muted small"></span></div>
  <div id="ires"></div>`;
  $("#scan").onclick = () => guard(scanImport);
  pollImport();
}
async function scanImport() {
  const kind = $("#ik").value;
  $("#ires").innerHTML = '<p class="muted">Scanning folders…</p>';
  const items = await api("/import/scan?kind=" + kind);
  window._imp = items;
  if (!items.length) { $("#ires").innerHTML = '<p class="muted">Nothing new found in the root folders. Check Settings → Root folders.</p>'; return; }
  $("#ires").innerHTML = `<div class="panel"><div class="toolbar">
      <label><input type="checkbox" id="iren"> Rename files to TRaSH naming</label>
      ${kind === "tv" ? `<label>Monitor <select id="imon"><option value="all">All episodes</option><option value="existing">Only episodes I have</option><option value="future">Future episodes</option><option value="none">None</option></select></label>`
        : `<label>Monitor <select id="imon"><option value="all">Yes</option><option value="none">No</option></select></label>`}
      <label><input type="checkbox" id="isearch"> Search for missing &amp; upgrades now</label>
      <span class="grow"></span><button class="primary" id="igo">Import selected</button></div>
    <p class="small muted" id="imatch">Matching 0/${items.length}…</p></div>
    <div class="table-wrap"><table><tr><th><input type="checkbox" id="iall" checked></th><th>Folder</th><th>Library</th><th>Files</th><th>Match</th></tr>
    ${items.map((c, i) => `<tr id="ir${i}"><td><input type="checkbox" class="isel" data-i="${i}" checked></td>
      <td>${esc(c.folder)}${c.loose ? ' <span class="chip warn">loose file</span>' : ""}</td><td>${tierChip(c.tier)}</td>
      <td class="small">${c.files} · ${size(c.size)}</td><td class="im">…</td></tr>`).join("")}</table></div>`;
  $("#iall").onchange = () => document.querySelectorAll(".isel").forEach((x) => (x.checked = $("#iall").checked));
  $("#igo").onclick = () => guard(startImport);
  for (let i = 0; i < items.length; i += 10) {
    const chunk = await api("/import/match", { method: "POST", body: { items: items.slice(i, i + 10) } });
    chunk.forEach((c, j) => { items[i + j] = c; drawMatch(i + j); });
    if ($("#imatch")) $("#imatch").textContent = `Matched ${Math.min(i + 10, items.length)}/${items.length}`;
  }
  if ($("#imatch")) $("#imatch").textContent = "Matching done. Check anything marked “guess” before importing.";
}
function drawMatch(i) {
  const c = window._imp[i], row = $("#ir" + i);
  if (!row) return;
  const m = c.match;
  const conf = { id: "ok", exact: "ok", guess: "warn" }[c.confidence] || "bad";
  row.querySelector(".im").innerHTML = m ? `${esc(m.title)} <span class="muted">(${m.year || "?"})</span> <span class="chip ${conf}">${c.confidence === "id" ? "ID match" : c.confidence}</span>
    ${m.in_library ? '<span class="chip bad">already in library</span>' : ""} <button class="small" onclick="fixMatch(${i})">Change</button>`
    : `<span class="chip bad">No match</span>${c.error ? ` <span class="rej">${esc(c.error)}</span>` : ""} <button class="small" onclick="fixMatch(${i})">Search</button>`;
  if (!m || m.in_library) row.querySelector(".isel").checked = false;
}
async function fixMatch(i) {
  const c = window._imp[i];
  const term = prompt("Search for:", c.match ? c.match.title : c.title);
  if (!term) return;
  const res = await guard(() => api(`/lookup/${c.kind === "movie" ? "movie" : "series"}?term=${encodeURIComponent(term)}`));
  if (!res) return;
  window._fix = res;
  modal(`<div class="toolbar"><h2 class="grow">Pick a match for “${esc(c.folder)}”</h2><button onclick="closeModal()">Close</button></div>
    ${res.map((r, j) => `<div class="result-row">${r.poster ? `<img src="${esc(r.poster)}" alt="">` : ""}<div class="grow"><b>${esc(r.title)}</b> <span class="muted">${r.year || ""}</span><p>${esc(r.overview)}</p></div>
    <button class="primary" onclick="pickMatch(${i}, ${j})">Use</button></div>`).join("") || '<p class="muted">No results.</p>'}`);
}
function pickMatch(i, j) {
  const r = window._fix[j], c = window._imp[i];
  c.match = { tmdb_id: r.tmdb_id, tvdb_id: r.tvdb_id, title: r.title, year: r.year, poster: r.poster, in_library: r.in_library };
  c.confidence = "exact";
  closeModal(); drawMatch(i);
  $("#ir" + i).querySelector(".isel").checked = !r.in_library;
}
async function startImport() {
  const sel = [...document.querySelectorAll(".isel:checked")].map((x) => window._imp[+x.dataset.i]).filter((c) => c.match);
  if (!sel.length) throw new Error("Select at least one matched item");
  const opts = { rename: $("#iren").checked, monitor: $("#imon").value, search: $("#isearch").checked };
  const items = sel.map((c) => ({ kind: c.kind, tier: c.tier, folder: c.folder, loose: c.loose, id: c.kind === "movie" ? c.match.tmdb_id : null, tvdb_id: c.kind === "tv" ? c.match.tvdb_id : null, tmdb_id: c.kind === "tv" ? c.match.tmdb_id : null, ...opts }));
  await api("/import", { method: "POST", body: { items } });
  toast(`Importing ${items.length} item(s)…`);
  pollImport();
}
async function pollImport() {
  const st = await api("/import/status").catch(() => null);
  const el = $("#ist");
  if (!st || !el) return;
  if (st.running) {
    el.textContent = `Importing ${st.done}/${st.total}${st.current ? " — " + st.current : ""}`;
    setTimeout(pollImport, 1500);
  } else if (st.total) {
    el.textContent = `Last import: ${st.imported}/${st.total} imported${st.errors.length ? `, ${st.errors.length} failed (see Activity → Events)` : ""}`;
    if (st.done === st.total && window._imp) { window._imp = null; setTimeout(() => guard(scanImport), 300); }
  }
}

async function renderNaming(el, s) {
  const tp = await api("/trash/profiles");
  if (!tp.loaded) { el.innerHTML = '<p class="muted">Sync TRaSH Guides first (Profiles tab).</p>'; return; }
  const nm = tp.naming;
  const sel = (k, obj) => `<select data-k="${k}">${Object.keys(obj || {}).map((o) => `<option ${s[k] === o ? "selected" : ""}>${o}</option>`).join("")}</select><div class="help"><code>${esc((obj || {})[s[k]] || "")}</code></div>`;
  el.innerHTML = `<p class="muted">Naming schemes come from TRaSH Guides' recommended Radarr/Sonarr formats.</p><div class="form">
    <label>Movie folder</label>${sel("naming_movie_folder", nm.movie.folder)}
    <label>Movie file</label>${sel("naming_movie_file", nm.movie.file)}
    <label>Series folder</label>${sel("naming_series_folder", nm.tv.series)}
    <label>Season folder</label>${sel("naming_season_folder", nm.tv.season)}
    <label>Episode file</label>${sel("naming_episode_file", (nm.tv.episodes || {}).standard)}
    <span></span><div><button class="primary" id="save">Save</button></div></div>
    <p class="small muted">Folder naming is applied when an item is added; file naming is applied on every import.</p>`;
  $("#save").onclick = () => guard(async () => { await api("/settings", { method: "PUT", body: collect(el) }); toast("Saved"); route(); });
}
routes.settings = (sub) => renderSettings(sub || "general");

// --------------------------------------------------------------- theme ---
function currentTheme() { try { return localStorage.getItem("omniarr-theme") || "system"; } catch { return "system"; } }
function setTheme(t) {
  try { if (t === "system") localStorage.removeItem("omniarr-theme"); else localStorage.setItem("omniarr-theme", t); } catch { /* private mode */ }
  if (t === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  document.querySelectorAll("[data-theme-choice]").forEach((b) => b.classList.toggle("active", b.dataset.themeChoice === t));
}
document.querySelectorAll("[data-theme-choice]").forEach((b) => (b.onclick = () => setTheme(b.dataset.themeChoice)));
setTheme(currentTheme());

// ----------------------------------------------------------- requests ---
const REQ_TABS = { all: "All", pending: "Pending", approved: "Approved", available: "Available", declined: "Declined" };
async function renderRequests(sub = "all") {
  if (!REQ_TABS[sub]) sub = "all";
  const rows = await api("/requests" + (sub === "all" ? "" : "?status=" + sub));
  const kindName = { movie: "Movie", tv: "TV show", collection: "Collection" };
  const statusChip = (r) => {
    const cls = { pending: "warn", approved: "", available: "ok", declined: "bad", failed: "bad" }[r.status];
    const p = r.progress && r.status === "approved" && r.progress.total ? ` ${r.progress.have}/${r.progress.total}` : "";
    return `<span class="chip ${cls}">${{ pending: "Waiting for approval", approved: "Approved — downloading", available: "Available", declined: "Declined", failed: "Failed" }[r.status]}${p}</span>`;
  };
  const link = (r) => !r.item_id || r.status === "pending" ? esc(r.title) : r.kind === "movie" ? `<a href="#/movie/${r.item_id}">${esc(r.title)}</a>` : r.kind === "tv" ? `<a href="#/show/${r.item_id}">${esc(r.title)}</a>` : `<a href="#/collections">${esc(r.title)}</a>`;
  view.innerHTML = `<div class="toolbar"><h2 class="grow">${isAdmin() ? "Requests" : "My requests"}</h2>
    <a href="#/discover"><button>Browse streaming</button></a><a href="#/add"><button class="primary">New request</button></a></div>
    <div class="tabs2">${Object.entries(REQ_TABS).map(([k, v]) => `<button class="${k === sub ? "active" : ""}" onclick="location.hash='#/requests/${k}'">${v}</button>`).join("")}</div>
    ${rows.length ? `<div class="table-wrap"><table><tr><th></th><th>Title</th><th>Library</th>${isAdmin() ? "<th>Requested by</th>" : ""}<th>Requested</th><th>Status</th><th></th></tr>
    ${rows.map((r) => `<tr><td>${r.poster ? `<img src="${esc(r.poster)}" alt="" style="width:42px;border-radius:4px">` : ""}</td>
      <td><b>${link(r)}</b> <span class="muted">${r.year || ""}</span><div class="small muted">${kindName[r.kind]}${r.kind === "collection" && r.options.tmdb_ids ? ` · ${r.options.tmdb_ids.length} movies` : ""}</div>
        ${r.note && r.note !== "Auto-approved" ? `<div class="small ${r.status === "declined" || r.status === "failed" ? "rej" : "muted"}">${esc(r.note)}</div>` : ""}</td>
      <td>${isAdmin() && r.status === "pending" ? `<select id="rt${r.id}">${["hd", "uhd", "both"].map((v) => `<option value="${v}" ${r.tier === v ? "selected" : ""}>${TIER_LABEL[v]}</option>`).join("")}</select>` : tierChip(r.tier)}</td>
      ${isAdmin() ? `<td>${esc(r.username || "deleted user")}</td>` : ""}
      <td class="small muted">${esc(r.created)}${r.decided_by_name ? `<br>${r.note === "Auto-approved" ? "auto-approved" : (r.status === "declined" ? "declined" : "approved") + " by " + esc(r.decided_by_name)}` : ""}</td>
      <td>${statusChip(r)}</td>
      <td>${r.status === "pending" ? (isAdmin()
        ? `<button class="small primary" onclick="approveReq(${r.id}, this)">Approve</button> <button class="small danger" onclick="declineReq(${r.id})">Decline</button>`
        : `<button class="small" onclick="cancelReq(${r.id})">Cancel</button>`) : (isAdmin() ? `<button class="small" title="Remove from the list" onclick="cancelReq(${r.id})">✕</button>` : "")}</td></tr>`).join("")}</table></div>`
    : `<p class="muted">${sub === "pending" ? "No requests waiting." : "No requests yet."}${isAdmin() ? "" : " Find something on the Add or Streaming pages and click Request."}</p>`}`;
}
async function approveReq(id, btn) {
  btn.disabled = true;
  await guard(async () => {
    const r = await api(`/requests/${id}/approve`, { method: "POST", body: { tier: $("#rt" + id) ? $("#rt" + id).value : null } });
    toast(r.status === "failed" ? `${r.title}: ${r.note}` : `Approved ${r.title}`, r.status === "failed");
  });
  refreshBadge(); route();
}
async function declineReq(id) {
  const note = prompt("Reason for declining (optional, shown to the user):");
  if (note === null) return;
  await guard(() => api(`/requests/${id}/decline`, { method: "POST", body: { note } }));
  refreshBadge(); route();
}
async function cancelReq(id) {
  if (!confirm(isAdmin() ? "Remove this request from the list? (Anything already added stays in the library.)" : "Cancel this request?")) return;
  await guard(() => api("/requests/" + id, { method: "DELETE" }));
  refreshBadge(); route();
}

// -------------------------------------------------------------- users ---
async function renderUsers(el) {
  const users = await api("/users");
  const tick = (u, k, label) => `<label class="small" style="color:var(--text)"><input type="checkbox" ${u[k] ? "checked" : ""} ${u.role === "admin" && k !== "enabled" ? "disabled" : ""} onchange="editUser(${u.id}, {${k}: this.checked})"> ${label}</label>`;
  el.innerHTML = `<p class="muted">Admins can do everything. Users can browse the library and request movies, shows and collections.
    <b>Auto-approve</b> adds a user's requests straight away; otherwise they wait in <a href="#/requests/pending">Requests</a> for an admin.
    <b>4K</b> lets a user request titles for the 4K library.</p>
    <div class="table-wrap"><table><tr><th>User</th><th>Role</th><th>Permissions</th><th>Last login</th><th></th></tr>
    ${users.map((u) => `<tr><td><b>${esc(u.username)}</b>${u.id === ME.id ? ' <span class="chip">you</span>' : ""}</td>
      <td><select onchange="editUser(${u.id}, {role: this.value})"><option value="user" ${u.role === "user" ? "selected" : ""}>User</option><option value="admin" ${u.role === "admin" ? "selected" : ""}>Admin</option></select></td>
      <td>${u.role === "admin" ? '<span class="small muted">Everything</span> ' : `${tick(u, "auto_approve", "Auto-approve requests")} ${tick(u, "allow_uhd", "Can request 4K")} `}${tick(u, "enabled", "Enabled")}</td>
      <td class="small muted">${esc(u.last_login || "never")}</td>
      <td><button class="small" onclick="resetPw(${u.id}, '${esc(u.username)}')">Set password</button> ${u.id === ME.id ? "" : `<button class="small danger" onclick="delUser(${u.id}, '${esc(u.username)}')">Delete</button>`}</td></tr>`).join("")}</table></div>
    <h3>Add user</h3><form class="form" id="nuf">
      <label>Username</label><input id="nu" autocomplete="off" required>
      <label>Password</label><input id="np" type="password" autocomplete="new-password" required><div class="help">At least 8 characters. They can change it later under Account.</div>
      <label>Role</label><select id="nr"><option value="user">User — can request</option><option value="admin">Admin — full access</option></select>
      <label>Auto-approve requests</label><input type="checkbox" id="na">
      <label>Can request 4K</label><input type="checkbox" id="n4">
      <span></span><div><button class="primary" type="submit">Create user</button></div></form>`;
  $("#nuf").onsubmit = (e) => { e.preventDefault(); guard(async () => {
    await api("/users", { method: "POST", body: { username: $("#nu").value.trim(), password: $("#np").value, role: $("#nr").value, auto_approve: $("#na").checked, allow_uhd: $("#n4").checked } });
    toast("User created"); route();
  }); };
}
async function editUser(id, body) { await guard(async () => { await api("/users/" + id, { method: "PUT", body }); toast("Saved"); }); route(); }
async function resetPw(id, name) {
  const pw = prompt(`New password for ${name} (at least 8 characters):`);
  if (pw) await guard(async () => { await api("/users/" + id, { method: "PUT", body: { password: pw } }); toast("Password set — they'll need to log in again"); });
}
async function delUser(id, name) { if (confirm(`Delete ${name}? Their requests stay in the list.`)) await guard(async () => { await api("/users/" + id, { method: "DELETE" }); route(); }); }

// ---------------------------------------------------- login / accounts ---
function renderAuth(mode) {
  document.body.classList.add("logged-out");
  document.body.classList.remove("is-admin");
  $("#banner").innerHTML = "";
  const setup = mode === "setup";
  view.innerHTML = `<div class="auth-box panel">
    <h2>${setup ? "Welcome to Omniarr" : "Log in"}</h2>
    ${setup ? '<p class="muted">Create the admin account. Admins manage settings and approve requests; you can add more users afterwards under Settings → Users.</p>' : ""}
    <form id="authf" class="form" style="grid-template-columns:1fr">
      <label for="au">Username</label><input id="au" autocomplete="username" required>
      <label for="ap">Password</label><input id="ap" type="password" autocomplete="${setup ? "new-password" : "current-password"}" required>
      ${setup ? '<label for="ap2">Repeat password</label><input id="ap2" type="password" autocomplete="new-password" required><div class="small muted">At least 8 characters.</div>' : ""}
      <button class="primary" type="submit">${setup ? "Create admin account" : "Log in"}</button>
      <div id="aerr" class="rej"></div></form></div>`;
  $("#au").focus();
  $("#authf").onsubmit = async (e) => {
    e.preventDefault();
    $("#aerr").textContent = "";
    if (setup && $("#ap").value !== $("#ap2").value) { $("#aerr").textContent = "Passwords don't match"; return; }
    try {
      await api(setup ? "/auth/setup" : "/auth/login", { method: "POST", body: { username: $("#au").value.trim(), password: $("#ap").value } });
      if (!location.hash || location.hash === "#/") location.hash = "#/movies";
      boot();
    } catch (err) { $("#aerr").textContent = err.message; }
  };
}
function renderUserMenu() {
  $("#usermenu").innerHTML = `<span class="small muted">${esc(ME.username)}${isAdmin() ? ' <span class="chip">admin</span>' : ""}</span>
    <button class="small" onclick="changePassword()">Account</button><button class="small" onclick="logout()">Log out</button>`;
}
async function logout() { await api("/auth/logout", { method: "POST" }).catch(() => {}); ME = null; renderAuth("login"); }
function changePassword() {
  modal(`<div class="toolbar"><h2 class="grow">Change password</h2><button onclick="closeModal()">Close</button></div>
    <form id="pwf" class="form"><label>Current password</label><input id="pw0" type="password" autocomplete="current-password" required>
    <label>New password</label><input id="pw1" type="password" autocomplete="new-password" required>
    <label>Repeat new password</label><input id="pw2" type="password" autocomplete="new-password" required>
    <span></span><div><button class="primary" type="submit">Change password</button></div></form>`);
  $("#pwf").onsubmit = (e) => { e.preventDefault(); guard(async () => {
    if ($("#pw1").value !== $("#pw2").value) throw new Error("New passwords don't match");
    await api("/auth/password", { method: "POST", body: { current: $("#pw0").value, new: $("#pw1").value } });
    closeModal(); toast("Password changed");
  }); };
}
async function refreshBadge() {
  if (!ME) return;
  const me = await api("/auth/me").catch(() => null);
  if (!me) return;
  ME = { ...ME, ...me };
  const n = isAdmin() ? me.pending_requests || 0 : 0;
  $("#reqbadge").textContent = n ? String(n) : "";
}
async function boot() {
  const st = await fetch("/api/auth/status").then((r) => r.json()).catch(() => ({}));
  if (st.setup_needed) return renderAuth("setup");
  if (!st.user) return renderAuth("login");
  ME = st.user;
  document.body.classList.remove("logged-out");
  document.body.classList.toggle("is-admin", isAdmin());
  renderUserMenu();
  checkBanner();
  refreshBadge();
  route();
}
setInterval(() => { if (isAdmin()) refreshBadge(); }, 60000);
boot();
