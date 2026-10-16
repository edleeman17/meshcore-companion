// MeshCore Companion PWA: chats, contacts, map, signal stats and radio panel over the bridge API (/api/*).
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = async (path, opts = {}) => {
  const r = await fetch("/api" + path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
// Over-the-air round-trips to a node (login, status, telemetry, trace, path
// discovery) often time out on the first try over a marginal link — retry.
const apiOTA = async (path, opts = {}, tries = 4) => {
  let last;
  for (let i = 0; i < tries; i++) {
    try { return await api(path, opts); } catch (e) { last = e; }
  }
  throw last;
};
// 1-byte vs 2-byte MeshCore path hashes, from the hash size seen in a node's adverts.
const hashBadge = (hs) => hs === 2
  ? `<span class="chip q-good" title="2-byte path hashes (newer MeshCore — far fewer routing collisions as the mesh grows)">2-byte</span>`
  : hs === 1 ? `<span class="chip" title="Classic 1-byte path hashes">1-byte</span>` : "";
const ago = (ms) => {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
};
const hhmm = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
// One SNR scale for the whole app (SF8 decodes down to about -12.5 dB).
const SNR_SCALE = "0 dB or more strong · down to −7 good · −7 to −12 marginal · below −12 mostly lost";
const snrGrade = (snr) => snr == null ? { cls: "", word: "", leg: "wait" }
  : snr >= 0 ? { cls: "q-good", word: "strong", leg: "ok" }
  : snr >= -7 ? { cls: "q-good", word: "good", leg: "ok" }
  : snr >= -12 ? { cls: "q-ok", word: "marginal", leg: "maybe" }
  : { cls: "q-bad", word: "weak", leg: "bad" };
const qClass = (snr) => snrGrade(snr).cls;
const qWord = (snr) => snrGrade(snr).word;
const hopsText = (n) => n == null ? "" : n === 0 ? "direct" : `${n} hop${n === 1 ? "" : "s"}`;
const TYPES = { 1: "person", 2: "repeater", 3: "room", 4: "sensor" };
// First visible character of a name, emoji-safe ("📡basecamp" → "📡", not half a surrogate pair).
const seg = typeof Intl !== "undefined" && Intl.Segmenter ? new Intl.Segmenter() : null;
const initial = (name) => {
  const s = String(name || "?").trim() || "?";
  const first = seg ? seg.segment(s)[Symbol.iterator]().next().value.segment : Array.from(s)[0];
  return /\p{L}/u.test(first) ? first.toUpperCase() : first;
};
// A stable colour per name (FNV-1a hash -> hue), so the same person looks the
// same in channels, chats and contacts. Saturation/lightness come from the theme.
const hueOf = (name) => {
  let h = 2166136261;
  for (const ch of String(name || "")) h = Math.imul(h ^ ch.codePointAt(0), 16777619);
  return (h >>> 0) % 360;
};
const tint = (name) => ` style="--h:${hueOf(name)}"`;
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

// Toasts replace alert(): iOS home-screen apps can suppress native dialogs.
let toastTimer;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast " + kind; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), kind === "err" ? 5000 : 3000);
}
const fail = (err, what = "") => toast(`${what}${what ? ": " : ""}${err?.message || err}`, "err");

// In-app confirm (replaces confirm()). Resolves true/false.
function confirmSheet(title, body, ok = "Confirm", danger = false) {
  return new Promise((resolve) => {
    showSheet(`<h3 id="sheet-title" tabindex="-1" autofocus>${esc(title)}</h3>${body ? `<p>${esc(body)}</p>` : ""}
      <div class="actions"><button type="button" class="btn ${danger ? "danger" : "primary"}" id="cf-ok">${esc(ok)}</button>
      <button type="button" class="btn" id="cf-no">Cancel</button></div>`, { noClose: true });
    const done = (v) => { $("#sheet").close(); resolve(v); };
    $("#cf-ok").onclick = () => done(true);
    $("#cf-no").onclick = () => done(false);
    $("#sheet").addEventListener("cancel", () => resolve(false), { once: true });
  });
}

// Each navigation bumps this; async views drop their render if you've moved on.
let nav = 0;

const S = { state: null, contacts: [], convs: [], route: ["chats"], thread: [], range: "24h", stats: null };

// ---------- routing ----------
// Desktop (wide) layout: a sidebar instead of bottom tabs, and Chats/Contacts as
// two panes (list left, open conversation/contact right). Below the breakpoint
// everything behaves exactly as the phone layout.
const wideMq = matchMedia("(min-width: 64rem)");
let wide = wideMq.matches;
document.body.classList.toggle("wide", wide);
const isSplit = () => document.body.classList.contains("split");

let settleTimer;
function go() {
  nav++;
  $("#sheet").open && $("#sheet").close();
  closeMsgMenu();
  // Lists animate in once per visit, not on every live refresh.
  document.querySelectorAll(".list.settled").forEach((l) => l.classList.remove("settled"));
  clearTimeout(settleTimer);
  settleTimer = setTimeout(() => document.querySelectorAll(".list").forEach((l) => l.classList.add("settled")), 900);
  const parts = (location.hash.replace(/^#\/?/, "") || "chats").split("/").map(decodeURIComponent);
  S.route = parts;
  const tab = ["dm", "chan"].includes(parts[0]) ? "chats" : parts[0] === "contact" ? "contacts"
    : parts[0] === "placement" ? "signal" : parts[0];
  const split = wide && (tab === "chats" || tab === "contacts");
  document.querySelectorAll(".view").forEach((v) => (v.hidden = true));
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
  $("#back").hidden = !(["dm", "chan", "contact", "placement"].includes(parts[0]) || (["signal", "radio"].includes(parts[0]) && parts[1])) || split;
  $("#info").hidden = !["dm", "chan"].includes(parts[0]);
  $("#thread-info").hidden = true;
  document.body.classList.toggle("in-thread", ["dm", "chan"].includes(parts[0]));
  document.body.classList.toggle("in-map", tab === "map");
  document.body.classList.toggle("split", split);
  // All views share one scroll container: start every non-conversation view at
  // the top (conversations jump to the newest message themselves). Split panes
  // scroll on their own, so the list keeps its place.
  if (split) { $("#v-contact").scrollTop = 0; $("#v-pick").scrollTop = 0; }
  else if (!["dm", "chan"].includes(parts[0])) document.querySelector("main").scrollTop = 0;
  if (split) {
    $(tab === "chats" ? "#v-chats" : "#v-contacts").hidden = false;
    if (tab === "chats") loadConvs(); else loadContacts();
    if (parts[0] === "chats" || parts[0] === "contacts") {
      $("#v-pick").hidden = false;
      $("#pick-text").textContent = tab === "chats" ? "Pick a conversation." : "Pick a contact to see its details.";
      $("#title").textContent = tab === "chats" ? "Chats" : "Contacts";
    }
  }
  if (parts[0] === "dm" || parts[0] === "chan") openThread(parts[0], parts[1]);
  else if (parts[0] === "contact") openContact(parts[1]);
  else if (parts[0] === "placement") { $("#v-placement").hidden = false; $("#title").textContent = "Placement"; openPlacement(); }
  else if (tab === "contacts") { if (!split) { $("#v-contacts").hidden = false; $("#title").textContent = "Contacts"; loadContacts(); } }
  else if (tab === "map") { $("#v-map").hidden = false; $("#title").textContent = "Map"; showMap(); }
  else if (tab === "signal") { showSub("signal", parts[1]); loadStats(); if (parts[1] === "packets") loadPackets(); }
  else if (tab === "radio") { showSub("radio", parts[1]); renderRadio(); fillSettings(); fillFilters(); loadStats(); renderBatteryList(); }
  else if (!split) { $("#v-chats").hidden = false; $("#title").textContent = "MeshCore"; loadConvs(); }
}
// Signal and Radio open on a short menu; each feature is its own page (#/signal/charts).
function showSub(view, sub) {
  const sec = $(`#v-${view}`);
  sec.hidden = false;
  const page = sub && sec.querySelector(`.subpage[data-sub="${CSS.escape(sub)}"]`);
  sec.querySelector(".sub-home").hidden = !!page;
  sec.querySelectorAll(".subpage").forEach((d) => (d.hidden = d !== page));
  $("#title").textContent = page ? page.dataset.title : view === "signal" ? "Signal" : "Radio";
  page?.querySelector(".range-slot")?.append($("#range"));   // one range picker, shared
}

wideMq.addEventListener?.("change", () => {
  wide = wideMq.matches;
  document.body.classList.toggle("wide", wide);
  go();
  window.dispatchEvent(new Event("resize"));   // charts re-measure
});

// Refresh what's on screen without re-routing or moving the scroll position
// (used when the app comes back to the foreground or the stream recovers).
async function refreshView() {
  const [view, id] = S.route, my = nav;
  try {
    if (view === "dm" || view === "chan") {
      const rows = await api(`/messages/${view}/${encodeURIComponent(id)}${showHidden ? "?hidden=true" : ""}`);
      if (my !== nav) return;
      S.thread = rows; renderThread(); markRead(view, id);
      if (isSplit()) loadConvs();
    } else if (view === "contacts" || (view === "contact" && isSplit())) loadContacts();
    else if (view === "signal" || view === "radio") { if (view === "radio") renderRadio(); loadStats(); }
    else if (view === "map") showMap();
    else if (view === "placement") openPlacement();
    else if (view !== "contact") loadConvs();
  } catch { /* offline: the banner says so */ }
}
window.addEventListener("hashchange", go);
$("#back").onclick = () => (location.hash = ["signal", "radio"].includes(S.route[0]) ? `#/${S.route[0]}`
  : { contact: "#/contacts", placement: "#/signal" }[S.route[0]] || "#/chats");
// Keyboard (desktop): Esc closes the open conversation/contact, "/" jumps to search.
document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || $("#sheet").open) return;
  if (e.target.closest?.("input, textarea, select, [contenteditable]")) return;
  if (e.key === "Escape" && ["dm", "chan", "contact", "placement"].includes(S.route[0])) { e.preventDefault(); $("#back").click(); }
  else if (e.key === "/" && ["chats", "dm", "chan"].includes(S.route[0]) && !$("#v-chats").hidden) { e.preventDefault(); $("#search").focus(); }
});

// ---------- chats ----------
const contactName = (prefix) => S.contacts.find((c) => c.prefix === prefix)?.name || prefix.slice(0, 8);
const chanName = (idx) => S.state?.channels?.[idx] || (idx === "0" ? "Public" : `Channel ${idx}`);

// "Last heard": MeshCore has no presence, so this is the latest sign of life the
// bridge saw — an advert, a DM, a channel post or a delivery receipt.
function presence(c) {
  const ts = c?.heard?.[0] ? c.heard[0] * 1000 : 0;
  if (!ts) return null;
  const age = Date.now() - ts;
  return { cls: age < 15 * 60e3 ? "p-on" : age < 2 * 3600e3 ? "p-recent" : "p-off",
    text: `heard ${age < 60e3 ? "just now" : ago(ts) + " ago"}`, how: c.heard[1] || "" };
}
const presenceDot = (c) => { const p = presence(c); return p ? `<i class="pdot ${p.cls}" title="${esc(p.text + (p.how ? " · " + p.how : ""))}"></i>` : ""; };
const contactByPrefix = (prefix) => S.contacts.find((c) => c.prefix === prefix);

// A DM on a saved (direct) route arrives with an empty path: each repeater strips
// itself as it forwards. Our own saved route to them, reversed, is the best guess.
const likelyRoute = (m) => {
  const r = m.kind === "dm" && m.direction === "in" && m.path_len == null && contactByPrefix(m.peer)?.route;
  return r?.length ? [...r].reverse() : null;
};

const routeText = (c) => c.path_len < 0 ? "no fixed route" : c.path_len === 0 ? "direct" : `${c.path_len} hop${c.path_len === 1 ? "" : "s"}`;
// The list's preview line says who spoke last: "You: …", or the sender in a channel.
function lastLine(c) {
  if (!c.last_text) return "&nbsp;";
  const r = parseReaction(c.last_text);
  const who = c.last_dir === "out" ? "You" : c.kind === "chan" ? c.last_sender : "";
  const by = !who ? "" : who === "You" ? `<span class="by">You${r ? "" : ":"}</span> `
    : `<span class="by tint"${tint(who)}>${esc(who)}${r ? "" : ":"}</span> `;
  return by + (r ? `${who ? "reacted" : "Reacted"} ${esc(r.emoji)}` : esc(c.last_text));
}
const convRow = (c) => {
  const name = c.kind === "chan" ? chanName(c.peer) : c.name || contactName(c.peer);
  const unread = c.unread ? ` unread` : "";
  const cur = S.route[0] === c.kind && S.route[1] === c.peer ? ` aria-current="true"` : "";
  return `<a href="#/${c.kind}/${esc(c.peer)}" class="${unread}"${cur}>
    ${c.kind === "chan" ? `<span class="badge chan">#</span>` : `<span class="badge tint"${tint(name)}>${esc(initial(name)) + presenceDot(contactByPrefix(c.peer))}</span>`}
    <span class="meta"><div class="name">${esc(name)}</div><div class="last">${lastLine(c)}</div></span>
    <span class="when">${c.last_ts ? ago(c.last_ts) : ""}${c.mentions || c.unread ? `<span class="marks">${c.mentions ? '<span class="at" title="You were mentioned">@</span>' : ""}${c.unread ? `<span class="count-badge">${c.unread}</span>` : ""}</span>` : ""}</span></a>`;
};

async function loadConvs() {
  try { [S.convs, S.contacts] = await Promise.all([api("/conversations"), api("/contacts")]); }
  catch (err) {
    if (!S.convs.length) $("#convs").innerHTML = `<p class="empty">Can't reach the bridge right now. Your chats will appear when it's back.</p>`;
    return;
  }
  const notify = new Set(S.state?.notify_channels || []);
  setBadge(S.convs.reduce((n, c) => n + (c.kind === "dm" || notify.has(c.peer) ? c.unread : c.mentions), 0));
  const seen = new Set(S.convs.map((c) => `${c.kind}:${c.peer}`));
  const active = [...S.convs].sort((a, b) => (b.last_ts || 0) - (a.last_ts || 0));
  const quiet = Object.keys(S.state?.channels || {}).filter((idx) => !seen.has(`chan:${idx}`))
    .map((idx) => ({ kind: "chan", peer: idx, last_text: "No messages yet", last_ts: 0 }));
  $("#convs").innerHTML = active.map(convRow).join("")
    || `<p class="empty">No messages yet. Join a channel with <b>+ Channel</b>, or message a contact below.</p>`;
  $("#quiet-wrap").hidden = !quiet.length;
  $("#quiet-count").textContent = quiet.length ? `(${quiet.length})` : "";
  $("#quiet").innerHTML = quiet.map(convRow).join("");
  loadDiscovered();
  const people = S.contacts.filter((c) => c.type === 1 && !seen.has(`dm:${c.prefix}`));
  $("#people-wrap").hidden = !people.length;
  $("#people").innerHTML = people.map((c) => `<a href="#/dm/${c.prefix}"${S.route[0] === "dm" && S.route[1] === c.prefix ? ` aria-current="true"` : ""}>
      <span class="badge tint"${tint(c.name)}>${esc(initial(c.name))}</span>
      <span class="meta"><div class="name">${esc(c.name)}</div><div class="last">${routeText(c)}</div></span></a>`).join("");
}
const loadConvsSoon = debounce(() => { if (S.route[0] === "chats" || (isSplit() && ["dm", "chan"].includes(S.route[0]))) loadConvs(); }, 500);

const setBadge = (n) => { try { n ? navigator.setAppBadge?.(n) : navigator.clearAppBadge?.(); } catch { /* unsupported */ } };
const markRead = (kind, peer) => api("/read", { method: "POST", body: JSON.stringify({ kind, peer }) })
  .then((r) => { setBadge(r.badge); if (isSplit()) loadConvsSoon(); }).catch(() => {});

let searchTimer;
$("#search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = $("#search").value.trim();
  const searching = q.length >= 2;
  $("#results").hidden = !searching; $("#chat-home").hidden = searching;
  $("#people-wrap").hidden = searching || !$("#people").children.length;
  if (!searching) return;
  searchTimer = setTimeout(async () => {
    try {
      const rows = await api(`/search?q=${encodeURIComponent(q)}`);
      $("#results").innerHTML = rows.map((m) => `<a href="#/${m.kind}/${esc(m.peer)}">
        ${m.kind === "chan" ? `<span class="badge chan">#</span>` : `<span class="badge tint"${tint(m.where)}>${esc(initial(m.where))}</span>`}
        <span class="meta"><div class="name">${esc(m.where)}${m.sender && m.kind === "chan" ? ` · ${esc(m.sender)}` : ""}</div>
        <div class="last">${esc(m.text)}</div></span><span class="when">${ago(m.ts)}</span></a>`).join("") || `<p class="empty">No messages match "${esc(q)}".</p>`;
    } catch (err) { fail(err, "Search failed"); }
  }, 250);
});

async function loadDiscovered() {
  let rows;
  try { rows = await api("/channels/discovered"); } catch { return; }
  const found = rows.filter((r) => r.name), unknown = rows.filter((r) => !r.name);
  $("#discovered").innerHTML = found.map((r) => `<label>
      <span class="badge chan">#</span>
      <span class="meta"><div class="name">${esc(r.name)}</div>
      <div class="last">${r.count} message${r.count === 1 ? "" : "s"} heard · last ${ago(r.last_ts)} ago</div></span>
      <button class="btn primary sm" data-join="${esc(r.name)}">Join</button></label>`).join("")
    + (unknown.length ? `<p class="hint">${unknown.length} more active channel${unknown.length === 1 ? " is" : "s are"} private or under a name nobody's guessed yet (${unknown.reduce((n, r) => n + r.count, 0)} messages heard). Try a name:</p>` : "")
    || `<p class="hint">Listening… public channels appear here as they're heard.</p>`;
  $("#disc-note").textContent = found.length ? `(${found.length} to join)` : "";
}
$("#discovered").addEventListener("click", async (e) => {
  const name = e.target.dataset.join; if (!name) return;
  e.preventDefault();
  try { const r = await api("/channels", { method: "POST", body: JSON.stringify({ name }) }); await refreshState(); toast(`Joined ${name}`); location.hash = `#/chan/${r.idx}`; }
  catch (err) { fail(err, "Couldn't join"); }
});
$("#guess").onsubmit = async (e) => {
  e.preventDefault();
  const name = $("#guess-name").value.trim(); if (!name) return;
  try {
    const r = await api("/channels/guess", { method: "POST", body: JSON.stringify({ name }) });
    toast(r.found ? `${r.name} is active — ${r.count} messages heard. Tap Join.` : `${r.name} hasn't been heard yet. It'll be checked against new traffic.`);
    $("#guess-name").value = ""; loadDiscovered();
  } catch (err) { fail(err, "Couldn't check"); }
};

let showHidden = false;
async function loadHiddenBar(kind, peer) {
  let hidden = 0;
  try { ({ hidden } = await api(`/messages/${kind}/${encodeURIComponent(peer)}/hidden-count`)); } catch { /* optional */ }
  if (S.route[0] !== kind || S.route[1] !== peer) return;
  const bar = $("#hidden-bar");
  bar.hidden = !hidden;
  bar.innerHTML = hidden ? `${hidden} message${hidden === 1 ? "" : "s"} hidden by spam filters · <a href="#" id="toggle-hidden">${showHidden ? "hide them" : "show"}</a>` : "";
  $("#toggle-hidden")?.addEventListener("click", (e) => { e.preventDefault(); showHidden = !showHidden; openThread(kind, peer); });
}

async function openThread(kind, peer) {
  const my = nav;
  $("#v-thread").hidden = false;
  showSignToggle();
  $("#title").textContent = kind === "chan" ? chanName(peer) : contactName(peer);
  const pr = kind === "dm" && presence(contactByPrefix(peer));
  if (pr) $("#title").insertAdjacentHTML("beforeend", ` <small class="presence ${pr.cls}" title="${esc(pr.how)}">${esc(pr.text)}</small>`);
  if (!S.thread.length || S.thread[0]?.peer !== peer) $("#msgs").innerHTML = `<p class="empty">Loading…</p>`;
  if (S.threadKey !== `${kind}:${peer}`) { S.threadKey = `${kind}:${peer}`; S.seen = null; clearReply(); }
  let last = 0;
  try { last = (await api(`/read/${kind}/${encodeURIComponent(peer)}`)).last_read || 0; } catch { /* offline */ }
  if (my !== nav) return;
  S.lastRead = last;
  markRead(kind, peer);   // advances read to now; S.lastRead holds where we were
  let rows;
  try { rows = await api(`/messages/${kind}/${encodeURIComponent(peer)}${showHidden ? "?hidden=true" : ""}`); }
  catch { if (my === nav) $("#msgs").innerHTML = `<p class="empty">Can't load messages — the bridge isn't reachable.</p>`; return; }
  if (my !== nav) return;
  S.thread = rows;
  renderThread(true);
  loadHiddenBar(kind, peer);
}

// ---------- reactions ----------
// MeshCore has no reaction packet, so a reaction is an ordinary short message any
// client can read: "@[Alice] 👍" in a channel (or a room), just "👍" in a DM, plus
// ` "quote…"` when it's for an older message than their latest. This app folds
// them under the message they're for; other apps show them as a normal message.
const REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"];
const QUOTE_MAX = 24;
const EMOJI_RE = /^(?:\p{Extended_Pictographic}|\p{Emoji_Modifier}|\p{Regional_Indicator}|\uFE0F|\u200D)+$/u;
const REACT_RE = /^(?:@\[([^\]]{1,32})\] )?(\S{1,16})(?: "(.{1,40})")?$/su;
const isEmoji = (t) => EMOJI_RE.test(t) && (!seg || [...seg.segment(t)].length === 1);
function parseReaction(t) {
  const m = REACT_RE.exec(String(t || "").trim());
  if (!m || !isEmoji(m[2])) return null;
  return { to: m[1] || null, emoji: m[2], quote: m[3] ? m[3].replace(/…$/, "") : null };
}
// The message a reaction (row i of rows, parsed as p) is for: the latest earlier
// message from whoever it names (you, if it names your radio), matching the quote.
function reactionTarget(rows, r, p, i) {
  const me = S.state?.name;
  const from = (m) => p.to ? (p.to === me ? m.direction === "out" : m.direction === "in" && m.sender === p.to)
    : r.kind === "chan" ? m.sender !== r.sender || m.direction !== r.direction
    : m.direction !== r.direction;
  for (let j = i - 1; j >= Math.max(0, i - 300); j--) {
    const m = rows[j];
    if (from(m) && !parseReaction(m.text) && (!p.quote || m.text.startsWith(p.quote))) return m;
  }
  return null;
}
// Whose name a reaction to m carries: channel and room posts name their author.
const reactTo = (m) => m.kind === "chan" || (m.sender && m.sender !== contactName(m.peer)) ? m.sender : null;
function reactionText(m, emoji) {
  const to = reactTo(m), p = { to, emoji, quote: null };
  const mine = { kind: m.kind, direction: "out", sender: S.state?.name };
  const latest = reactionTarget(S.thread, mine, p, S.thread.length) === m;
  const quote = latest ? "" : m.text.length > QUOTE_MAX ? m.text.slice(0, QUOTE_MAX).trimEnd() + "…" : m.text;
  return (to ? `@[${to}] ` : "") + emoji + (quote ? ` "${quote}"` : "");
}
// Reactions grouped onto their target message id; their own rows are hidden.
// A reaction of yours that failed stays a normal bubble so it can be resent.
function groupReactions(rows) {
  const on = new Map(), hide = new Set();
  rows.forEach((m, i) => {
    const p = parseReaction(m.text);
    if (!p || (m.direction === "out" && m.status === "failed")) return;
    const t = reactionTarget(rows, m, p, i); if (!t) return;
    hide.add(m.id);
    if (!on.has(t.id)) on.set(t.id, []);
    on.get(t.id).push({ id: m.id, emoji: p.emoji, mine: m.direction === "out", pending: m.status === "sending",
      who: m.direction === "out" ? "You" : m.sender || contactName(m.peer) });
  });
  return { on, hide };
}
function reactsHtml(list, fresh) {
  if (!list?.length) return "";
  const by = new Map();
  for (const r of list) { if (!by.has(r.emoji)) by.set(r.emoji, []); by.get(r.emoji).push(r); }
  return `<div class="reacts">${[...by].map(([emoji, rs]) => {
    const cls = (rs.some((r) => r.mine) ? " mine" : "") + (rs.every((r) => r.pending) ? " pending" : "");
    const anim = rs.some((r) => fresh.has(r.id)) ? "" : ` style="animation:none"`;
    return `<span class="react${cls}"${anim} title="${esc(rs.map((r) => r.who).join(", "))}">${emoji}${rs.length > 1 ? `<b>${rs.length}</b>` : ""}</span>`;
  }).join("")}</div>`;
}

// Inline contact cards people paste in chat: <64-hex pubkey:type:name>
const CARD_RE = /&lt;([0-9a-fA-F]{64}):([1-4]):([^&]{1,32}?)&gt;/g;
function renderText(t) {
  return esc(t).replace(CARD_RE, (_, key, type, name) => {
    const known = S.contacts.some((c) => c.public_key === key.toLowerCase());
    return `<span class="card-chip" data-card-key="${key}" data-card-type="${type}" data-card-name="${name}">
      👤 ${name} <span class="tag">${TYPES[type] || ""}</span> ${known ? "<b>✓ in contacts</b>" : "<button>Add</button>"}</span>`;
  });
}

function dmStatusTip(m) {
  if (m.kind !== "dm") return m.status;
  if (m.status === "delivered") return "Their radio sent back a receipt — it arrived. No need to resend.";
  if (m.status === "sending") return "Sent to the mesh, waiting for a receipt.";
  if (m.status === "failed") return m.echoes
    ? "A repeater rebroadcast it, but no receipt came back. Probably arrived; the receipt can get lost over several hops. Resend only if they go quiet."
    : "No repeater was heard passing it on, so it never left your area. Safe to resend.";
  return m.status;
}

function chanStatusTip(m) {
  if (m.status === "failed") return "The radio couldn't send it. Resend.";
  if (m.echoes) return `${m.echoes} repeater${m.echoes === 1 ? "" : "s"} rebroadcast it — it's out on the mesh. Channels have no read receipt, so this is as confirmed as delivery gets.`;
  if (Date.now() - m.ts < 60000) return "Sent to the mesh, waiting to hear a repeater carry it.";
  return "No repeater was heard rebroadcasting it, so it didn't get out of your area. Resend.";
}

// Signed-message badge. A valid signature only proves a key signed it; the
// anti-spoof part is key_state (match = key you pinned for this name, changed =
// a DIFFERENT key, the spoof alarm).
function signedChip(m) {
  if (!m.signed) return "";
  if (m.signed_ok === 1) {
    if (m.key_state === "mine") return ` <span class="verified" title="Signed with your radio's key">🔏</span>`;
    if (m.key_state === "match") return ` <span class="verified" title="Signed — key matches the one you pinned for ${esc(m.signed_name || m.sender)}">✓🔏</span>`;
    if (m.key_state === "changed") return ` <span class="chip q-bad" title="Valid signature, but a DIFFERENT key than you pinned for ${esc(m.sender)} — possible impersonator">⚠ key changed</span>`;
    return ` <span class="chip" title="Signed and valid, but you haven't pinned a key for ${esc(m.sender)} yet — open to verify">🔏 signed</span>`;
  }
  if (m.signed_ok === 0) return ` <span class="chip q-bad" title="Carries a signature that does NOT check out — ignore it">🔏✗</span>`;
  // legacy room-relay attribution (no crypto)
  return ` <span class="chip" title="Relayed with author tag ${esc(m.signed_name || m.signed)}">🔏</span>`;
}

function msgHtml(m, reacts = null, fresh = new Set()) {
  const dmFailWord = m.echoes ? "no receipt" : "not sent";
  const stWord = m.kind === "dm"
    ? { sending: "sending…", delivered: "✓ delivered", failed: `✗ ${dmFailWord}`, sent: "sent" }
    : { sending: "sending…", delivered: "✓ delivered", failed: "✗ failed", sent: "sent" };
  const st = m.direction === "out"
    ? `<span class="st-${m.status}" title="${esc(dmStatusTip(m))}">${stWord[m.status] || ""}</span>` : "";
  const sig = m.snr != null ? `<span class="chip ${qClass(m.snr)}">${m.snr} dB</span>` : "";
  const warn = m.warn ? `<span class="chip q-bad" title="${esc(m.warn)}">⚠ unusual route</span>` : "";
  const hops = m.direction === "in"
    ? (m.path_len != null ? `<span class="chip chip-hops">${hopsText(m.path_len)}</span>`
      : (m.kind === "dm" ? (() => { const lr = likelyRoute(m);
          return `<span class="chip chip-hops" title="${lr ? `Likely via ${esc(lr.join(" → "))} (your saved route to them, reversed)` : "Arrived on an established direct route — no flood path to count"}">via saved route${lr ? ` · ${hopsText(lr.length)}` : ""}</span>`; })() : ""))
    : "";
  const rtt = m.rtt_ms ? `<span>${(m.rtt_ms / 1000).toFixed(1)}s</span>` : "";
  const dmEcho = m.direction === "out" && m.kind === "dm" && m.status !== "delivered" && m.echoes != null
    ? `<span>${m.echoes ? `↺ repeated ×${m.echoes}` : "↺ not repeated yet"}</span>` : "";
  const chanStale = m.kind === "chan" && m.status === "sent" && !m.echoes && Date.now() - m.ts > 60000;
  const echo = m.direction === "out" && m.kind === "chan" && (m.status === "sent" || m.status === "failed")
    ? (m.status === "failed"
        ? `<span class="st-failed" title="${esc(chanStatusTip(m))}">✗ not sent</span>`
        : m.echoes
          ? `<span class="st-delivered" title="${esc(chanStatusTip(m))}">✓ on mesh · ↺×${m.echoes}</span>`
          : chanStale
            ? `<span class="st-failed" title="${esc(chanStatusTip(m))}">✗ not picked up</span>`
            : `<span class="st-sending" title="${esc(chanStatusTip(m))}">sending…</span>`)
    : "";
  const roomAuthor = m.direction === "in" && m.kind === "dm" && m.sender && m.sender !== contactName(m.peer);
  const badge = m.verified ? ` <span class="verified" title="Route matches the sender you confirmed">✓</span>` : "";
  const signedBadge = signedChip(m);
  // In a 1:1 DM the sender is obvious — only channels and room posts get a name line.
  const showWho = m.direction === "in" && (m.kind === "chan" || roomAuthor) && m.sender;
  const who = showWho ? `<div class="who tint" data-mention="${esc(m.sender)}"${tint(m.sender)}>${esc(m.sender)}${badge}${signedBadge}</div>` : "";
  const dmBadges = !showWho && m.direction === "in" ? badge + signedBadge : "";
  const canResend = m.direction === "out" && (m.status === "failed" ||
    (m.kind === "chan" && m.status === "sent" && !m.echoes && Date.now() - m.ts > 60000));
  const resendLabel = m.kind === "dm" && m.status === "failed" && m.echoes ? "↻ Send again anyway" : "↻ Resend";
  const resend = canResend ? `<div class="msg-actions"><button type="button" class="resend" data-resend="${m.id}">${resendLabel}</button></div>` : "";
  const failHint = m.kind === "dm" && m.status === "failed"
    ? `<p class="hint-line">${m.echoes
        ? "Reached the mesh, but no receipt came back. It most likely arrived — resend only if they don't reply."
        : "No repeater picked this up, so it didn't get out. Resend."}</p>` : "";
  const chanHint = m.direction === "out" && (chanStale || (m.kind === "chan" && m.status === "failed"))
    ? `<p class="hint-line">No repeater was heard carrying this, so it didn't get out. Resend.</p>` : "";
  const arrive = fresh.has(m.id) ? " arrive" : "";
  return `<div class="msg ${m.direction}${m.mention ? " mention" : ""}${m.hidden ? " is-hidden" : ""}${arrive}" data-id="${m.id}"><span class="swipe-ico" aria-hidden="true">↩</span>${who}<div class="text">${renderText(m.text)}</div>
    ${reactsHtml(reacts, fresh)}<div class="foot">${dmBadges}${warn}${sig}${hops}${rtt}<span>${hhmm(m.ts)}</span>${m.kind === "chan" && m.direction === "out" ? echo : dmEcho + st}</div>${failHint}${chanHint}${resend}</div>`;
}

function showSheet(html, { noClose = false } = {}) {
  const d = $("#sheet");
  $("#sheet-body").innerHTML = html;
  d.querySelector(":scope > form[method=dialog]").hidden = noClose;   // not a form inside the body
  if (!d.open) d.showModal();
  d.scrollTop = 0;
  // Focus the heading so the dialog doesn't jump to (and ring) the first button.
  (d.querySelector("[autofocus]") || d.querySelector("h3"))?.focus({ preventScroll: true });
}

// A small "where did this message go" picture: you → repeaters → them, with each
// leg coloured by what we actually know (a repeat heard, a receipt back, the SNR).
function jrNode(ico, lbl) { return `<div class="jr-node"><div class="jr-ico">${ico}</div><div class="jr-lbl">${lbl}</div></div>`; }
function jrLeg(cls, lbl) { return `<div class="jr-leg jr-${cls}"><span class="jr-leg-lbl">${lbl}</span><span class="jr-line"></span></div>`; }

function journeyHtml(m) {
  const chan = m.kind === "chan";
  if (m.direction === "out" && !chan) {
    const e = m.echoes || 0, got = m.status === "delivered", sending = m.status === "sending";
    const l1 = e ? "ok" : (sending ? "wait" : "bad");
    const l2 = got ? "ok" : (e ? "maybe" : (sending ? "wait" : "bad"));
    const say = got ? "Delivered — their radio sent back a receipt, so it definitely arrived."
      : sending ? "Sent onto the mesh — waiting to hear a receipt."
      : e ? `${e} repeater${e === 1 ? "" : "s"} rebroadcast it onto the mesh, but no receipt came back across the hops. It most likely arrived — the receipt just got lost on the way home.`
      : "No repeater was heard carrying it, so it never left your area. Resend.";
    return `<div class="journey">${jrNode("📱", "You")}${jrLeg(l1, e ? `↺ ×${e}` : (sending ? "sending…" : "no repeat"))}${jrNode("📡", "repeaters")}${jrLeg(l2, got ? "✓ receipt" : (e ? "likely got it" : (sending ? "…" : "✗ no receipt")))}${jrNode("👤", esc(contactName(m.peer)))}</div><p class="jr-say">${say}</p>`;
  }
  if (m.direction === "out" && chan) {
    const e = m.echoes || 0, sending = m.status === "sending" || (!e && Date.now() - m.ts < 60000);
    const l1 = e ? "ok" : (sending ? "wait" : "bad");
    const say = e ? `${e} repeater${e === 1 ? "" : "s"} rebroadcast it, so it's out on the mesh. Channels have no receipt — this is as confirmed as a broadcast gets.`
      : sending ? "Sent — waiting to hear a repeater carry it."
      : "No repeater was heard carrying it, so it didn't get out. Resend.";
    return `<div class="journey">${jrNode("📱", "You")}${jrLeg(l1, e ? `↺ ×${e}` : (sending ? "sending…" : "✗ no repeat"))}${jrNode("📡", "mesh")}</div><p class="jr-say">${say}</p>`;
  }
  const snr = m.snr, q = snrGrade(snr).leg;
  const lr = likelyRoute(m);
  const leglbl = (m.path_len != null ? hopsText(m.path_len) : (m.kind === "dm" ? "saved route" : "heard")) + (snr != null ? ` · ${snr} dB` : "");
  if (lr) {   // their side first: them → likely repeaters → you
    const say = `Came down a saved route, so the packet carries no hop list (each repeater removes itself as it forwards). ` +
      `Your saved route to them, reversed, is the likely path.${snr != null ? ` You heard the last hop at SNR ${snr} dB (${qWord(snr)}).` : ""}`;
    return `<div class="journey">${jrNode("👤", esc(m.sender || contactName(m.peer)))}${jrLeg("wait", "likely")}${(lr.length > 3 ? [lr[0], `+${lr.length - 2} more`, lr.at(-1)] : lr).map((n) => jrNode("📡", esc(n))).join(jrLeg("wait", ""))}${jrLeg(q, snr != null ? `${snr} dB` : "")}${jrNode("📱", "You")}</div><p class="jr-say">${say}</p>`;
  }
  const say = snr == null ? "Received." :
    `You heard this at SNR ${snr} dB (${qWord(snr)})${m.path_len ? ", over " + hopsText(m.path_len) : ""}. That's how well you hear them — not how well they hear you.`;
  return `<div class="journey">${jrNode("👤", esc(m.sender || contactName(m.peer)))}${jrLeg(q, leglbl)}${jrNode("📱", "You")}</div><p class="jr-say">${say}</p>`;
}

async function renderRoute(c) {
  const key = c.public_key;
  const cur = c.path_len < 0 ? "Flooding — radio picks a path each time (reliable, but slower and spams the mesh)"
    : c.path_len === 0 ? "Direct — 0 hops"
    : `Pinned ${c.path_len}-hop route`;
  let reps = [];
  try { reps = (await api("/nearby?hours=168")).filter((n) => n.type === 2 && n.pubkey); } catch { /* offline */ }
  const seen = new Set(), opts = [];
  for (const r of reps.sort((a, b) => (b.snr ?? -99) - (a.snr ?? -99))) {
    const h = r.pubkey.slice(0, 2);
    if (seen.has(h)) continue; seen.add(h);
    opts.push(`<option value="${h}">${esc(r.name || r.pubkey.slice(0, 8))} (${h}${r.snr != null ? ", " + r.snr + "dB" : ""})</option>`);
  }
  if (S.route[0] !== "contact" || S.route[1] !== key) return;
  $("#c-route").innerHTML = `<p class="hint">Now: <b>${cur}</b>. Normally the radio learns routes by itself — pin one only to force a path it won't pick on its own.</p>
    <form id="route-form" class="form">
      <label>Add a repeater hop (strongest first)
        <select id="route-rep"><option value="">+ pick a repeater…</option>${opts.join("")}</select></label>
      <label>Path — 2 hex characters per hop, you → them
        <input id="route-path" placeholder="e.g. dd (1 hop) or ddf3 (2 hops)" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
      <div class="row-actions"><button type="submit" class="btn primary">Pin route</button><button type="button" class="btn" id="route-flood">↺ Back to flood</button></div>
      <small class="hint">A pinned route skips flooding — faster and less mesh traffic, but only works while every hop is reachable. Go back to flood if it gets flaky.</small>
    </form>`;
  $("#route-rep").onchange = (e) => { if (e.target.value) { $("#route-path").value += e.target.value; e.target.value = ""; } };
  const setPath = async (path, done) => {
    try {
      await api(`/contacts/${key}/path`, { method: "POST", body: JSON.stringify({ path }) });
      S.contacts = await api("/contacts"); toast(done); openContact(key);
    } catch (err) { fail(err, "Couldn't change route"); }
  };
  $("#route-form").onsubmit = (e) => { e.preventDefault(); setPath($("#route-path").value.trim(), "Route pinned ✓"); };
  $("#route-flood").onclick = () => setPath("", "Back to flood ✓");
}

function msgDetails(m) {
  const enc = m.kind === "dm"
    ? "🔒 End-to-end. Encrypted to your key — only your radio's private key can unlock it. The radio decrypted it; over the air it was ciphertext that repeaters relayed without reading."
    : "🔑 Channel key. Anyone holding this channel's shared password can read it — encrypted on air, but not private between two people.";
  const summary = {
    From: m.direction === "out" ? "You" : m.sender || contactName(m.peer),
    [m.direction === "out" ? "Sent" : "Received"]: new Date(m.ts).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }),
    Route: m.path_names ? `${m.path_names} → you` : null,
  };
  const rows = {
    Encryption: enc,
    "From key": m.direction === "in" && m.kind === "dm" && m.peer ? m.peer.slice(0, 12) : null,
    "Sender's clock": m.sender_ts ? `${new Date(m.sender_ts * 1000).toLocaleTimeString()} (${Math.round(m.ts / 1000 - m.sender_ts)}s before arrival)` : null,
    Signal: m.snr != null ? `SNR ${m.snr} dB (${qWord(m.snr)})${m.rssi != null ? ` · RSSI ${m.rssi} dBm` : ""}` : null,
    Hops: m.direction === "in" ? (m.path_len != null ? hopsText(m.path_len)
      : (m.kind === "dm" ? (likelyRoute(m) ? `via saved route · likely ${likelyRoute(m).join(" → ")} (your route to them, reversed; the packet itself carries no hop list)`
        : "via saved route (sent down an established path, so no hop list)") : null)) : null,
    "⚠ Warning": m.warn || null,
    Signed: !m.signed ? null
      : m.signed_ok === 0 ? `🔏✗ carries a signature that does not check out — ignore it`
      : m.key_state === "mine" ? `🔏 signed with your radio's key`
      : m.key_state === "match" ? `✓🔏 valid — key matches the one you pinned for ${m.sender}`
      : m.key_state === "changed" ? `⚠ valid signature, but a DIFFERENT key than you pinned for ${m.sender} — possible impersonator`
      : m.key_state === "unpinned" ? `🔏 valid signature by key ${m.signed} — not pinned to ${m.sender} yet`
      : `🔏 by ${m.signed_name || "key " + m.signed}`,
    Verified: m.verified ? "✓ route matches the sender you confirmed" : null,
    Status: m.status,
    "Round trip": m.rtt_ms ? `${(m.rtt_ms / 1000).toFixed(1)} s` : null,
    Repeats: m.echoes != null && m.direction === "out" ? `${m.echoes}${m.echo_via ? " via " + m.echo_via : ""}` : null,
    Length: `${new TextEncoder().encode(m.text).length} / 140 bytes`,
  };
  // Signed message with a valid signature → pin its KEY (cryptographic). Unsigned
  // → the weaker route-origin confirm. Don't offer when already the matching pin.
  const canPinKey = m.direction === "in" && m.kind === "chan" && m.sender && m.signed_ok === 1
    && m.key_state !== "match" && m.key_state !== "mine";
  const canTrust = !canPinKey && m.direction === "in" && m.kind === "chan" && m.sender
    && (m.path || m.path_len === 0) && !m.verified;
  const canMap = m.direction === "in" && ((m.path && m.path.length) || m.path_len === 0);
  // DM the sender of a channel post, if their name matches a person/room you have
  // as a contact (so we hold their key). Prefer a key-verified signer.
  const dmTo = m.kind === "chan" && m.direction === "in" && m.sender
    ? S.contacts.find((c) => (c.type === 1 || c.type === 3) && c.name === m.sender)
    : null;
  // Fixed path: reverse this inbound DM's hop order to get a candidate route back.
  const canPin = m.kind === "dm" && m.direction === "in" && m.path && m.path.length >= 2;
  const revPath = canPin ? (m.path.match(/../g) || []).reverse().join("") : "";
  const kv = (o) => Object.entries(o).filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
  showSheet(`<h3 id="sheet-title" tabindex="-1" autofocus>Message details</h3>${journeyHtml(m)}
    <dl class="kv">${kv(summary)}</dl>
    ${dmTo ? `<div class="actions"><button type="button" class="btn primary" id="dm-btn">💬 Message ${esc(dmTo.name)} privately</button></div>` : ""}
    ${m.kind === "chan" && m.direction === "in" && m.sender && !dmTo ? `<p class="hint">To DM ${esc(m.sender)}, add them as a contact first (Contacts → Nearby, or scan their QR) — a channel name alone isn't enough to reach them.</p>` : ""}
    ${m.direction === "in" && callsignOf(summary.From) ? `<div class="actions"><button type="button" class="btn" data-call="${callsignOf(summary.From)}">📻 ${callsignOf(summary.From)} — ham profile</button></div>` : ""}
    ${canMap ? `<div class="actions"><button type="button" class="btn" id="routemap-btn">🗺 Show route on map</button></div>` : ""}
    ${canPin ? `<div class="actions"><button type="button" class="btn" id="pin-btn">🧭 Pin this route (${revPath.length / 2} hops)</button>
      <button type="button" class="btn" id="unpin-btn">↺ Back to flood</button></div>
      <p class="hint">Pins a fixed route to ${esc(m.sender || contactName(m.peer))} instead of flooding. If replies start failing, go back to flood.</p>` : ""}
    ${canPinKey ? `<div class="actions"><button type="button" class="btn primary" id="trust-btn">${m.key_state === "changed" ? `⚠ Pin this new key as ${esc(m.sender)}` : `🔏 Pin ${esc(m.sender)}'s key`}</button></div>
      <p class="hint">${m.key_state === "changed"
        ? `This signature is valid but signed with a <b>different key</b> than you pinned for ${esc(m.sender)}. Only pin it if you're sure they changed radios — otherwise it may be an impersonator.`
        : `Remembers this signing key as the real ${esc(m.sender)}. Future posts in their name signed by a different key are then flagged.`}</p>` : ""}
    ${canTrust ? `<div class="actions"><button type="button" class="btn" id="trust-btn">✓ This is really ${esc(m.sender)}</button>
      <button type="button" class="btn" id="untrust-btn">Forget confirmed route</button></div>
      <p class="hint">Channel names can be faked. Confirming remembers where the real ${esc(m.sender)} sends from, and flags their name arriving from anywhere else.</p>` : ""}
    <details class="fold"><summary>Technical details</summary><dl class="kv">${kv(rows)}</dl></details>`);
  $("#dm-btn")?.addEventListener("click", () => { $("#sheet").close(); location.hash = `#/dm/${dmTo.prefix}`; });
  $("#routemap-btn")?.addEventListener("click", () => routeMap(m).catch((err) => fail(err, "Couldn't map the route")));
  const sheetAct = (id, fn, ok, what) => $(id)?.addEventListener("click", async () => {
    try { await fn(); $("#sheet").close(); if (ok) toast(ok); } catch (err) { fail(err, what); }
  });
  sheetAct("#pin-btn", () => api(`/contacts/${m.peer}/path`, { method: "POST", body: JSON.stringify({ path: revPath }) }),
    `Pinned a ${revPath.length / 2}-hop route to ${m.sender || contactName(m.peer)}`, "Couldn't pin");
  sheetAct("#unpin-btn", () => api(`/contacts/${m.peer}/path`, { method: "POST", body: JSON.stringify({ path: "" }) }),
    "Back to flood routing", "Couldn't reset");
  sheetAct("#trust-btn", async () => { await api("/trust", { method: "POST", body: JSON.stringify({ message_id: m.id, trust: true }) }); refreshView(); },
    `${m.sender} confirmed`, "Couldn't confirm");
  sheetAct("#untrust-btn", async () => { await api("/trust", { method: "POST", body: JSON.stringify({ message_id: m.id, trust: false }) }); refreshView(); },
    "Confirmed route forgotten", "Couldn't forget");
}

// Draw a message's hops on a map: sender → each repeater in the path → you.
// Hop bytes (1 per repeater) are matched to a known node's pubkey for coords;
// unlocated hops are listed but skipped on the line.
async function routeMap(m) {
  if (!S.contacts.length) S.contacts = await api("/contacts");
  let nearby = []; try { nearby = await api("/nearby?hours=720"); } catch { /* optional */ }
  const byKey = {};
  for (const c of S.contacts) if (c.lat || c.lon) byKey[c.public_key.toLowerCase()] = { name: c.name, lat: c.lat, lon: c.lon };
  for (const n of nearby) if ((n.lat || n.lon) && !byKey[n.pubkey]) byKey[n.pubkey] = { name: n.name, lat: n.lat, lon: n.lon };
  const findHop = (hop) => { const k = Object.keys(byKey).find((x) => x.startsWith(hop.toLowerCase())); return k ? byKey[k] : null; };

  const pts = [];
  let sender = null;
  if (m.kind === "dm") { const c = S.contacts.find((x) => x.prefix === m.peer); if (c && (c.lat || c.lon)) sender = { name: c.name, lat: c.lat, lon: c.lon }; }
  else sender = Object.values(byKey).find((v) => v.name === m.sender) || null;
  if (sender) pts.push({ ...sender, kind: "sender" });

  const hops = m.path ? (m.path.match(/.{1,2}/g) || []) : [];
  const missing = [];
  for (const h of hops) { const r = findHop(h); if (r) pts.push({ ...r, kind: "hop" }); else missing.push(h); }
  if (S.state?.lat) pts.push({ name: "You", lat: S.state.lat, lon: S.state.lon, kind: "me" });

  if (pts.length < 2) return toast("Not enough located nodes on this route to map it"
    + (missing.length ? ` — ${missing.length} hop(s) have no known position yet.` : "."));

  const km = (a, b) => {                       // great-circle distance, km
    const R = 6371, r = Math.PI / 180;
    const dLat = (b.lat - a.lat) * r, dLon = (b.lon - a.lon) * r;
    const s = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(s));
  };
  const legs = pts.slice(1).map((p, i) => km(pts[i], p));
  const total = legs.reduce((a, b) => a + b, 0);
  const me = pts.find((p) => p.kind === "me"), snd = pts.find((p) => p.kind === "sender");
  const crow = me && snd ? km(snd, me) : null;

  showSheet(`<h3 id="sheet-title" tabindex="-1" autofocus>Route on map</h3><div id="route-map" class="route-map"></div>
    <p class="hint"><b>${total.toFixed(1)} km</b> over ${legs.length} hop${legs.length !== 1 ? "s" : ""}${crow != null ? ` · ${crow.toFixed(1)} km straight-line` : ""}${missing.length ? ` · ${missing.length} hop(s) not located` : ""}<br>${esc(m.path_names || "direct")}</p>`);
  const rmap = L.map($("#route-map"), { zoomControl: true });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(rmap);
  const line = pts.map((p) => [p.lat, p.lon]);
  const col = { sender: css("--link"), hop: css("--warn"), me: css("--fg") };
  pts.forEach((p, i) => L.circleMarker([p.lat, p.lon], { radius: p.kind === "hop" ? 6 : 8, color: col[p.kind] || css("--muted"), weight: 2, fillOpacity: .6 })
    .bindPopup(`<b>${esc(p.name)}</b><br>${p.kind === "sender" ? "sender" : p.kind === "me" ? "you" : "repeater"}${p.kind === "hop" ? " · hop " + i : ""}${i ? ` · ${legs[i - 1].toFixed(1)} km from previous` : ""}`).addTo(rmap));
  // Animate the message hopping leg by leg, labelling each leg's distance.
  const grow = L.polyline([], { color: css("--accent"), weight: 3, opacity: .9, dashArray: "6 4" }).addTo(rmap);
  let i = 0;
  const step = () => {
    if (!document.getElementById("route-map")) return;   // sheet closed mid-animation
    grow.addLatLng(line[i]);
    if (i > 0) {
      const a = line[i - 1], b = line[i], mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
      L.marker(mid, { icon: L.divIcon({ className: "leg-km", html: legs[i - 1].toFixed(1) + " km" }), interactive: false }).addTo(rmap);
    }
    if (++i < line.length) setTimeout(step, 500);
  };
  setTimeout(() => { rmap.invalidateSize(); rmap.fitBounds(line, { padding: [30, 30], maxZoom: 14 }); step(); }, 60);
}

$("#msgs").addEventListener("click", async (e) => {
  const card = e.target.closest("[data-card-key]");
  if (card) {
    e.stopPropagation();
    if (e.target.tagName !== "BUTTON") return;
    const btn = e.target; btn.disabled = true; btn.textContent = "…";
    try {
      await api("/contacts/card", { method: "POST", body: JSON.stringify({
        public_key: card.dataset.cardKey, type: +card.dataset.cardType, name: card.dataset.cardName }) });
      S.contacts = await api("/contacts"); renderThread(); toast(`Added ${card.dataset.cardName}`);
    } catch (err) { fail(err, "Couldn't add"); btn.disabled = false; btn.textContent = "Add"; }
    return;
  }
  const rs = e.target.closest("[data-resend]");
  if (rs) {
    e.stopPropagation();
    if (!await confirmSheet("Resend this message?", "If the first one did arrive, they'll see it twice.", "Resend")) return;
    rs.disabled = true; rs.textContent = "…";
    try { await api(`/messages/${rs.dataset.resend}/resend`, { method: "POST" }); toast("Resent"); }
    catch (err) { fail(err, "Couldn't resend"); rs.disabled = false; rs.textContent = "↻ Resend"; }
    return;
  }
  const at = e.target.closest("[data-mention]");
  if (at) {
    text.value = `@[${at.dataset.mention}] ` + text.value; text.focus(); text.dispatchEvent(new Event("input"));
    return;
  }
  const el = e.target.closest(".msg"); if (!el) return;
  const m = S.thread.find((x) => x.id === +el.dataset.id); if (m) msgDetails(m);
});

// ---------- message gestures: hold (or right-click) for reactions, swipe right to reply ----------
let gest = null, menuMsg = null, eatClick = false;
const msgOf = (el) => S.thread.find((x) => x.id === +el.dataset.id);
const swallowClick = () => { eatClick = true; setTimeout(() => (eatClick = false), 400); };
$("#msgs").addEventListener("click", (e) => { if (eatClick) { e.stopPropagation(); e.preventDefault(); eatClick = false; } }, true);
$("#msgs").addEventListener("pointerdown", (e) => {
  const el = e.target.closest(".msg");
  if (!el || e.button > 0 || e.target.closest("button, a, [data-card-key]")) return;
  gest = { el, id: e.pointerId, x: e.clientX, y: e.clientY, dx: 0, swipe: false, touch: e.pointerType !== "mouse" };
  gest.timer = setTimeout(() => {
    if (!gest || gest.swipe) return;
    gest.held = true;          // the tap that ends this hold mustn't also open details
    navigator.vibrate?.(8);
    openMsgMenu(el);
  }, 450);
});
$("#msgs").addEventListener("pointermove", (e) => {
  if (!gest || e.pointerId !== gest.id || gest.held) return;
  const dx = e.clientX - gest.x, dy = e.clientY - gest.y;
  if (!gest.swipe) {
    if (Math.hypot(dx, dy) < 8) return;
    clearTimeout(gest.timer);
    if (!gest.touch || dx < 0 || Math.abs(dx) < Math.abs(dy) * 1.5) { gest = null; return; }   // a scroll, not a swipe
    gest.swipe = true; gest.el.classList.add("swiping");
  }
  gest.dx = Math.max(0, Math.min(dx, 96));
  const pull = gest.dx < 60 ? gest.dx : 60 + (gest.dx - 60) * .35;   // resists past the trigger point
  gest.el.style.transform = `translateX(${pull}px)`;
  gest.el.querySelector(".swipe-ico").style.opacity = Math.min(1, gest.dx / 60);
  const ready = gest.dx >= 60;
  if (ready !== gest.el.classList.contains("swipe-ready")) { gest.el.classList.toggle("swipe-ready", ready); if (ready) navigator.vibrate?.(6); }
});
const endGesture = (e) => {
  if (!gest || e.pointerId !== gest.id) return;
  clearTimeout(gest.timer);
  const { el, swipe, dx, held } = gest; gest = null;
  if (held) return swallowClick();
  if (!swipe) return;
  swallowClick();
  el.classList.remove("swiping", "swipe-ready");
  el.style.transform = ""; el.querySelector(".swipe-ico").style.opacity = "";
  const m = msgOf(el);
  if (e.type === "pointerup" && dx >= 60 && m) startReply(m);
};
$("#msgs").addEventListener("pointerup", endGesture);
$("#msgs").addEventListener("pointercancel", endGesture);
$("#msgs").addEventListener("contextmenu", (e) => {
  const el = e.target.closest(".msg"); if (!el) return;
  e.preventDefault();
  if (menuMsg?.el !== el) openMsgMenu(el);
});

function closeMsgMenu() {
  if (!menuMsg) return;
  menuMsg.menu.remove(); menuMsg.el.classList.remove("held"); menuMsg = null;
}
function openMsgMenu(el) {
  closeMsgMenu();
  const m = msgOf(el); if (!m) return;
  const canReact = m.direction === "in" && (m.kind === "dm" || m.sender) && !parseReaction(m.text);
  const menu = document.createElement("div");
  menu.className = "react-menu"; menu.setAttribute("role", "menu");
  menu.innerHTML = (canReact ? `<div class="emojis">${REACTIONS.map((r) => `<button type="button" role="menuitem" data-react="${r}" aria-label="React ${r}">${r}</button>`).join("")}</div>` : "")
    + `<div class="acts"><button type="button" role="menuitem" data-act="reply">↩ Reply</button><button type="button" role="menuitem" data-act="copy">Copy</button><button type="button" role="menuitem" data-act="info">Details</button></div>`;
  document.body.append(menu);
  el.classList.add("held");
  menuMsg = { menu, el, m };
  // Above the message if it fits, else below; kept on screen.
  const r = el.getBoundingClientRect(), w = menu.offsetWidth, h = menu.offsetHeight;
  const left = Math.min(Math.max(8, m.direction === "out" ? r.right - w : r.left), innerWidth - w - 8);
  const above = r.top - h - 8 > 8;
  const top = above ? r.top - h - 8 : Math.min(r.bottom + 8, innerHeight - h - 8);
  Object.assign(menu.style, { left: left + "px", top: top + "px" });
  menu.style.setProperty("--oy", above ? "100%" : "0%");
  menu.style.setProperty("--ox", m.direction === "out" ? "100%" : "0%");
  menu.querySelector("button").focus({ preventScroll: true });
  menu.onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    closeMsgMenu();
    if (b.dataset.react) sendReaction(m, b.dataset.react);
    else if (b.dataset.act === "reply") startReply(m);
    else if (b.dataset.act === "info") msgDetails(m);
    else if (b.dataset.act === "copy") navigator.clipboard?.writeText(m.text).then(() => toast("Copied"), () => toast("Couldn't copy", "err"));
  };
}
document.addEventListener("pointerdown", (e) => { if (menuMsg && !menuMsg.menu.contains(e.target)) closeMsgMenu(); }, true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && menuMsg) { e.preventDefault(); closeMsgMenu(); } });
window.addEventListener("resize", closeMsgMenu);
for (const el of [document.querySelector("main"), $("#msgs")]) el.addEventListener("scroll", closeMsgMenu, { passive: true });

async function sendReaction(m, emoji) {
  const [kind, peer] = S.route;
  try { await api("/send", { method: "POST", body: JSON.stringify({ kind, to: peer, text: reactionText(m, emoji), sign: false }) }); }
  catch (err) { fail(err, "Reaction not sent"); }
}

// Replying: a bar above the composer shows what you're answering. In a channel
// (or room) the post starts with "@[Name] " so they get a mention; the quote
// itself is never sent, so it costs no bytes.
let reply = null;
const replyPrefix = () => reply && reply.direction === "in" && reactTo(reply) ? `@[${reactTo(reply)}] ` : "";
const withReply = (t) => { const pre = replyPrefix(); return pre && !t.startsWith(pre.trim()) ? pre + t : t; };
function startReply(m) {
  reply = m;
  const who = m.direction === "out" ? "You" : m.sender || contactName(m.peer);
  const bar = $("#reply-bar") || Object.assign(document.createElement("div"), { id: "reply-bar", className: "reply-bar" });
  bar.innerHTML = `<span class="rb-text">Replying to <b${who === "You" ? "" : ` class="tint"${tint(who)}`}>${esc(who)}</b> · ${esc(m.text)}</span>
    <button type="button" aria-label="Cancel reply">✕</button>`;
  bar.querySelector("button").onclick = () => { clearReply(); text.focus(); };
  bar.querySelector(".rb-text").onclick = () => {
    const el = $(`.msg[data-id="${m.id}"]`); if (!el) return;
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  };
  $("#composer").prepend(bar);
  text.focus(); text.dispatchEvent(new Event("input"));
}
function clearReply() {
  if (!reply && !$("#reply-bar")) return;
  reply = null; $("#reply-bar")?.remove(); text.dispatchEvent(new Event("input"));
}

const mainEl = document.querySelector("main");
// The conversation scrolls in <main> on phones, in its own pane on desktop.
const scroller = () => isSplit() ? $("#msgs") : mainEl;
const nearBottom = () => { const el = scroller(); return el.scrollHeight - el.scrollTop - el.clientHeight < 120; };
function renderThread(forceBottom = false) {
  const stick = forceBottom || nearBottom();
  const el = scroller(), keep = el.scrollTop;
  const lr = S.lastRead || 0;
  const { on, hide } = groupReactions(S.thread);
  const shown = S.thread.filter((m) => !hide.has(m.id));
  // Only messages (and reactions) that arrived since the last render animate in.
  const fresh = new Set(S.seen ? S.thread.filter((m) => !S.seen.has(m.id)).map((m) => m.id) : []);
  S.seen = new Set(S.thread.map((m) => m.id));
  // First message newer than where we last read — the divider goes before it,
  // but only when there's at least one older (read) message above it.
  const idx = lr ? shown.findIndex((m) => m.ts > lr) : -1;
  $("#msgs").innerHTML = shown.map((m, i) =>
    (idx > 0 && i === idx ? `<div class="unread-line"><span>New since you last read</span></div>` : "") + msgHtml(m, on.get(m.id), fresh)
  ).join("") || `<p class="empty">Say hello.</p>`;
  requestAnimationFrame(() => {
    const line = forceBottom && idx > 0 ? $(".unread-line") : null;
    if (line) line.scrollIntoView({ block: "center" });
    else el.scrollTop = stick ? 1e9 : keep;
  });
}

const LIMIT = 140, MAX_PARTS = 5, enc = new TextEncoder();
const blen = (s) => enc.encode(s).length;

// Mirror of the bridge splitter, used to preview the part count as you type.
function byteChunks(s, budget) {
  const out = []; let cur = "";
  for (let word of s.split(" ")) {
    while (blen(word) > budget) {
      let cut = word.length;
      while (blen(word.slice(0, cut)) > budget) cut--;
      if (cur) { out.push(cur); cur = ""; }
      out.push(word.slice(0, cut)); word = word.slice(cut);
    }
    const cand = cur ? cur + " " + word : word;
    if (blen(cand) <= budget) cur = cand;
    else { out.push(cur); cur = word; }
  }
  if (cur) out.push(cur);
  return out;
}
function splitMessage(t) {
  if (blen(t) <= LIMIT) return [t];
  let total = 2, chunks;
  for (;;) {
    const reserve = `[${total}/${total}] `.length;
    chunks = byteChunks(t, LIMIT - reserve);
    if (chunks.length <= total) break;
    total = chunks.length;
  }
  const n = chunks.length;
  return chunks.map((c, i) => `[${i + 1}/${n}] ${c}`);
}

const text = $("#text");
text.addEventListener("input", () => {
  const val = withReply(text.value);
  const n = text.value.trim() ? splitMessage(withReply(text.value.trim())).length : 1;
  const c = $("#count");
  const isChan = S.route?.[0] === "chan";
  c.classList.remove("over", "warn");
  c.removeAttribute("title");
  if (signOn && isChan) {
    const left = LIMIT - blen(val);
    c.textContent = `🔏 ${left}`;
    c.classList.toggle("over", left < 0 || n > 1);
    c.title = "A signed post must fit one packet.";
  } else if (n <= 1) {
    c.textContent = LIMIT - blen(val);
  } else if (isChan) {
    // Each part of a channel post floods every repeater. Warn here only.
    c.textContent = `${n} msgs ⚠`;
    c.classList.add(n > MAX_PARTS ? "over" : "warn");
    c.title = `${n} channel parts — each one floods the whole mesh. Keep it short or send as a DM.`;
  } else {
    c.textContent = `${n} msgs`;          // DM: routed, no flood warning
    c.classList.toggle("over", n > MAX_PARTS);
  }
  text.style.height = "auto"; text.style.height = text.scrollHeight + "px";
});
text.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
  else if (e.key === "Escape" && reply) { e.preventDefault(); e.stopPropagation(); clearReply(); }
});
$("#share-card").onclick = () => {
  const s = S.state;
  if (!s?.public_key) return toast("Radio not connected yet.", "err");
  const card = `<${s.public_key}:1:${(s.name || "me").slice(0, 31)}>`;
  const pos = text.selectionStart ?? text.value.length;
  text.value = (text.value.slice(0, pos) + (pos && !/\s$/.test(text.value.slice(0, pos)) ? " " : "") + card + " " + text.value.slice(pos)).slice(0, 200);
  text.focus(); text.dispatchEvent(new Event("input"));
};

// Sign toggle: only on channel posts. A signed post proves it's really you and
// must fit one packet (the signature eats ~100 bytes), so it can't be split.
const signBtn = $("#sign-toggle");
let signOn = false;
// The signature rides in a separate packet, so a signed post gets the full
// single-packet text length; it just can't be split across packets.
function setSign(on) {
  signOn = on;
  signBtn.classList.toggle("on", on);
  signBtn.setAttribute("aria-pressed", on ? "true" : "false");
  text.dispatchEvent(new Event("input"));   // refresh the counter for the signed budget
}
signBtn.onclick = () => setSign(!signOn);
function showSignToggle() {
  const isChan = S.route?.[0] === "chan";
  signBtn.hidden = !isChan;
  if (!isChan) setSign(false);
}

$("#composer").onsubmit = async (e) => {
  e.preventDefault();
  const [kind, peer] = S.route;
  const typed = text.value.trim();
  if (!typed) return;
  const body = withReply(typed);
  const sign = signOn && kind === "chan";
  if (sign && splitMessage(body).length > 1) return toast("Too long to sign — a signed post must fit one message.", "err");
  const btn = e.target.querySelector("button[type=submit]");
  btn.classList.remove("sent"); void btn.offsetWidth; btn.classList.add("sent");
  clearReply();
  text.value = ""; text.dispatchEvent(new Event("input"));
  requestAnimationFrame(() => (scroller().scrollTop = 1e9));
  try { await api("/send", { method: "POST", body: JSON.stringify({ kind, to: peer, text: body, sign }) }); }
  catch (err) { fail(err, "Not sent"); text.value = body; text.dispatchEvent(new Event("input")); }
};

// ---------- live stream ----------
let es = null, lastEvent = 0;
function connectStream() {
  try { es?.close(); } catch { /* already closed */ }
  es = new EventSource("/api/stream");
  lastEvent = Date.now();
  const on = (name, fn) => es.addEventListener(name, (e) => { lastEvent = Date.now(); fn(e); });
  on("ping", () => {});
  on("hidden", (e) => { const d = JSON.parse(e.data); if (S.route[0] === d.kind && S.route[1] === d.peer) loadHiddenBar(d.kind, d.peer); });
  on("status", (e) => {
    S.state = { ...S.state, ...JSON.parse(e.data) }; renderConn();
    if (S.route[0] === "radio") renderRadio();
    loadConvsSoon();
    if (S.route[0] === "chan") $("#title").textContent = chanName(S.route[1]);
  });
  on("message", (e) => onMessage(JSON.parse(e.data)));
  on("message_update", (e) => onMessage(JSON.parse(e.data), true));
  on("sample", (e) => { if (S.state) S.state.latest = JSON.parse(e.data); if (S.route[0] === "radio") renderRadio(); });
  on("packet", (e) => onPacket(JSON.parse(e.data)));
  on("contacts", () => { api("/contacts").then((c) => { S.contacts = c; }).catch(() => {}); loadContactsSoon(); });
  on("alert", (e) => { const d = JSON.parse(e.data); toast(`${d.title} — ${d.body}`); if (S.route[0] === "radio") renderBatteryList(); });
  on("channel_found", () => { if (S.route[0] === "chats") loadDiscovered(); });
  on("pending", () => { refreshState().catch(() => {}); loadContactsSoon(); });
  on("cli", (e) => { const d = JSON.parse(e.data); if (S.route[0] === "contact" && S.route[1].startsWith(d.prefix)) logConsole("← " + d.text); });
  es.onerror = () => {
    $("#conn").textContent = "Offline"; $("#conn").className = "pill down";
    $("#offline").hidden = false; $("#offline").textContent = "Can't reach the bridge — reconnecting…";
  };
  es.onopen = () => { lastEvent = Date.now(); refreshState().catch(() => {}); };
}

// iOS suspends the page in the background and silently kills the stream, so
// reconnect (and reload what's on screen) whenever we come back, and whenever
// nothing - not even the 20 s heartbeat - has arrived for 45 s.
function resync() { connectStream(); refreshView(); }
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") resync(); });
window.addEventListener("pageshow", (e) => { if (e.persisted) resync(); });
setInterval(() => { if (document.visibilityState === "visible" && Date.now() - lastEvent > 45000) resync(); }, 10000);

function onMessage(m, update = false) {
  const [kind, peer] = S.route;
  if (m.kind === kind && m.peer === peer) {
    const i = S.thread.findIndex((x) => x.id === m.id);
    if (i >= 0) S.thread[i] = m; else if (!update) S.thread.push(m);
    markRead(kind, peer);
    renderThread();
  } else loadConvsSoon();
}

function onPacket(p) {
  if (p.hop && p.snr != null) {
    PL.samples.push({ ts: p.ts, hop: p.hop, name: p.heard_from, snr: p.snr, rssi: p.rssi, rep: p.hop_rep !== false });
    if (S.route[0] === "placement") renderPlacementSoon();
  }
  if (S.route[0] === "map" && TR.on) trailPacket(p);
  if (PK.paused || (PK.type && (PK.type === "SIGNED" ? !p.signed : p.ptype !== PK.type))) return;
  const tb = $("#live tbody");
  tb.querySelector(".empty-row")?.remove();
  tb.insertAdjacentHTML("afterbegin", pktRow(p));
  while (tb.rows.length > 30) tb.deleteRow(-1);
}

// ---------- packet inspector ----------
// Every received frame is stored raw for a week; tap one in the live log to see
// its bytes, what each byte means, and (for channels we hold the key to) the text.
const PK = { paused: false, type: "" };
const pktRow = (p) => `<tr class="pkt" data-id="${p.id ?? ""}" tabindex="0"><td>${hhmm(p.ts)}</td><td>${esc(p.ptype)}${p.signed ? ` <span title="signed by ${esc(p.signed)}">✍️</span>` : ""}</td><td>${p.snr ?? ""}</td><td>${p.rssi ?? ""}</td><td>${esc(p.heard_from || "")}</td></tr>`;

async function loadPackets() {
  let rows;
  try { rows = await api(`/packets?limit=30${PK.type ? "&type=" + PK.type : ""}`); } catch { return; }
  $("#live tbody").innerHTML = rows.length ? rows.map(pktRow).join("")
    : `<tr class="empty-row"><td colspan="5" class="empty">${PK.type === "SIGNED" ? "No signed messages heard in the last week." : PK.type ? "None of these heard yet." : "Waiting for packets…"}</td></tr>`;
}

$("#pkt-type").onclick = (e) => {
  const b = e.target.closest("button"); if (!b) return;
  $("#pkt-type").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  PK.type = b.dataset.t; loadPackets();
};
$("#pkt-pause").onclick = () => {
  PK.paused = !PK.paused;
  $("#pkt-pause").textContent = PK.paused ? "Resume" : "Pause";
  $("#pkt-pause").setAttribute("aria-pressed", PK.paused);
  if (!PK.paused) loadPackets();   // catch up on what arrived while paused
};
$("#live tbody").onclick = (e) => { const id = e.target.closest("tr.pkt")?.dataset.id; if (id) openPacket(id); };
$("#live tbody").onkeydown = (e) => { if (e.key === "Enter") $("#live tbody").onclick(e); };

function hexDump(hex, fields) {
  const owner = [];
  fields.forEach((f, i) => { for (let b = f.start; b < f.end; b++) owner[b] = i; });
  let out = "";
  for (let b = 0; b < hex.length / 2; b++) {
    const i = owner[b], g = i == null ? "" : fields[i].group;
    out += `<span class="hb g-${g}"${i == null ? "" : ` data-f="${i}"`}>${hex.substr(b * 2, 2)}</span>${b % 16 === 15 ? "<br>" : " "}`;
  }
  return out;
}

const fieldRows = (fields, pre) => fields.map((f, i) => `<tr class="pf g-${f.group}" data-f="${pre}${i}">
  <td>${esc(f.name)}<small>${f.end - f.start} B</small></td><td><code>${esc(f.value.length > 48 ? f.value.slice(0, 48) + "…" : f.value)}</code>${f.note ? `<div class="hint">${esc(f.note)}</div>` : ""}</td></tr>`).join("");

async function openPacket(id) {
  let d;
  try { d = await api(`/packets/${id}`); } catch (err) { return fail(err, "Couldn't load that packet"); }
  const meta = [new Date(d.ts).toLocaleTimeString(), d.snr != null && `SNR ${d.snr} dB`, d.rssi != null && `RSSI ${d.rssi} dBm`, d.size != null && `${d.size} bytes`].filter(Boolean).join(" · ");
  showSheet(`<h3 id="sheet-title" tabindex="-1" autofocus>${esc(d.summary)}</h3>
    <p class="hint">${esc(meta)}</p>
    ${d.type ? `<p class="hint">${esc(d.type)}: ${esc(d.type_note)} · ${esc(d.route_note)}</p>` : ""}
    ${d.error ? `<p class="hint-line">Stopped decoding: ${esc(d.error)}</p>` : ""}
    ${d.hex ? `<div class="hexdump" aria-label="Packet bytes">${hexDump(d.hex, d.fields)}</div>
    <p class="pk-key"><span class="g-header">header</span> <span class="g-path">path</span> <span class="g-payload">payload</span></p>` : ""}
    <table class="tbl pk-fields"><tbody>${fieldRows(d.fields, "")}</tbody></table>
    ${d.decrypted ? `<h2 class="sub">Decrypted <small>${esc(d.channel)}</small></h2><table class="tbl pk-fields"><tbody>${fieldRows(d.decrypted, "p")}</tbody></table>` : ""}
    <div class="actions">${d.path || d.pubkey ? `<button class="btn" id="pk-map">Show route on map</button>` : ""}${d.hex ? `<button class="btn" id="pk-copy">Copy hex</button>` : ""}</div>`);
  const pm = $("#pk-map");
  if (pm) pm.onclick = () => {
    S.replay = { ...d, ptype: d.ptype, adv_key: d.pubkey, sender: d.text?.includes(": ") ? d.text.split(": ")[0] : null };
    $("#sheet").close(); location.hash = "#/map";
  };
  const body = $("#sheet-body");
  const light = (f) => {
    body.querySelectorAll(".hl").forEach((x) => x.classList.remove("hl"));
    if (f != null) body.querySelectorAll(`[data-f="${f}"]`).forEach((x) => x.classList.add("hl"));
  };
  body.querySelectorAll("[data-f]").forEach((x) => (x.onclick = () => {
    light(x.classList.contains("hl") ? null : x.dataset.f);
    if (x.classList.contains("hb")) body.querySelector(`tr[data-f="${x.dataset.f}"]`)?.scrollIntoView({ block: "nearest" });
  }));
  const cp = $("#pk-copy");
  if (cp) cp.onclick = async () => {
    try { await navigator.clipboard.writeText(d.hex); toast("Hex copied"); }
    catch { cp.replaceWith(Object.assign(document.createElement("textarea"), { className: "sel-box", readOnly: true, rows: 4, value: d.hex })); }
  };
}

// ---------- status + radio ----------
async function refreshState() {
  S.state = await api("/state"); renderConn();
  $("#pend-badge").hidden = !S.state.pending;
}

function renderConn() {
  const up = S.state?.connected;
  const ob = $("#offline");
  ob.hidden = !!up || !S.state;
  if (!up && S.state) ob.textContent = `Radio offline for ${ago(S.state.since)} — messages can't be sent.${S.state.last_error ? " " + S.state.last_error.replace(/\.?$/, ".") : ""}`;
  $("#conn").textContent = up ? "Online" : "Offline";
  $("#conn").className = "pill " + (up ? "up" : "down");
  $("#conn").title = up ? `Radio connected for ${ago(S.state.since)}` : S.state?.last_error || "Radio disconnected";
}

function renderRadio() {
  const s = S.state; if (!s) return;
  const L = s.latest || {}, r = s.radio || {};
  const v = L.battery?.level ? L.battery.level / 1000 : null;
  const air = L.core?.uptime_secs ? (((L.radio?.tx_air_secs || 0) / L.core.uptime_secs) * 100).toFixed(2) + "%" : "–";
  const g = L.radio ? snrGrade(L.radio.last_snr) : null;
  const tiles = [
    ["Link", s.connected ? "Connected" : "Offline", s.connected ? `${{ usb: "USB", wifi: "WiFi" }[s.link] || "Bluetooth"} · ${ago(s.since)}` : (s.last_error || `for ${ago(s.since)}`), s.connected ? "ok" : "bad"],
    ["Battery", v ? `${lipoPct(v)}%` : "–", v ? `${v.toFixed(2)} V` : "", v && lipoPct(v) < 20 ? "bad" : ""],
    ["Last signal", L.radio ? `${L.radio.last_snr} dB` : "–", g ? `${g.word} · ${L.radio.last_rssi} dBm` : "", g ? (g.cls === "q-bad" ? "bad" : "") : ""],
    ["Noise floor", L.radio ? `${L.radio.noise_floor} dBm` : "–", "lower is quieter", ""],
    ["Uptime", L.core?.uptime_secs ? `${(L.core.uptime_secs / 3600).toFixed(1)} h` : "–", "", ""],
    ["Airtime", air, "duty-cycle limit 10%", ""],
  ];
  $("#kv").innerHTML = tiles.map(([k, val, sub, tone]) =>
    `<div class="stat ${tone}"><div class="k">${k}</div><div class="v">${esc(val)}</div><div class="s">${esc(sub)}</div></div>`).join("")
    + `<dl class="kv span">${Object.entries({
      Name: s.name,
      Packets: L.packets ? `${L.packets.recv} received · ${L.packets.sent} sent · ${L.packets.recv_errors ?? 0} errors` : "–",
      "TX power": s.tx_power != null ? `${s.tx_power} dBm` : "–",
      Preset: r.radio_freq ? `${r.radio_freq} MHz · BW ${r.radio_bw} · SF${r.radio_sf} · CR${r.radio_cr}` : "–",
      Reconnects: s.reconnects,
    }).map(([k, x]) => `<dt>${k}</dt><dd>${esc(x)}</dd>`).join("")}</dl>`;
  $("#notify-back").checked = s.notify_back !== false;
  $("#notify-offline").checked = s.notify_offline !== false;
  const on = new Set(s.notify_channels || []);
  $("#chan-notify").innerHTML = Object.entries(s.channels || {}).map(([i, n]) =>
    `<label class="check"><input type="checkbox" data-chan="${i}" ${on.has(i) ? "checked" : ""}> ${esc(n)}</label>`).join("")
    || `<p class="hint">No channels yet.</p>`;
}

$("#chan-notify").addEventListener("change", async () => {
  const channels = [...document.querySelectorAll("[data-chan]:checked")].map((x) => x.dataset.chan);
  try { await api("/push/channels", { method: "POST", body: JSON.stringify({ channels }) }); S.state.notify_channels = channels; toast("Notification channels saved"); }
  catch (err) { fail(err, "Couldn't save"); }
});
const act = (btn, fn, done) => async () => {
  const label = btn.textContent; btn.disabled = true; btn.textContent = "…";
  try { const r = await fn(); toast(done ? done(r) : "Done"); }
  catch (err) { fail(err); }
  finally { btn.disabled = false; btn.textContent = label; }
};
$("#advert").onclick = act($("#advert"), () => api("/advert", { method: "POST" }), () => "Advert sent to nearby nodes");
$("#advert-flood").onclick = act($("#advert-flood"), () => api("/advert?flood=true", { method: "POST" }), () => "Advert flooded across the mesh");
$("#reboot").onclick = async () => {
  if (!await confirmSheet("Reboot the radio?", "The link drops for about 30 seconds, then reconnects by itself.", "Reboot", true)) return;
  try { await api("/reboot", { method: "POST" }); toast("Rebooting — back in about 30 s"); } catch (err) { fail(err, "Couldn't reboot"); }
};

// ---------- push ----------
const b64ToBytes = (b64) => {
  const s = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
};
$("#notify").onclick = async () => {
  if (!window.isSecureContext) {
    return toast("Notifications need HTTPS. See the README's setup guide: Tailscale is the easy way.", "err");
  }
  if (!("PushManager" in window) || !("serviceWorker" in navigator)) {
    return toast("On iPhone: Share → Add to Home Screen, open MeshCore from there, then tap this again.", "err");
  }
  try {
    if (!S.state?.vapid_public_key) throw new Error("the bridge has no push key configured");
    const perm = await Notification.requestPermission();
    if (perm !== "granted") return toast("Notifications are blocked for this app in your settings.", "err");
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(S.state.vapid_public_key) });
    await api("/push/subscribe", { method: "POST", body: JSON.stringify(sub) });
    $("#notify").textContent = "Notifications on ✓";
    toast("Notifications on");
  } catch (err) { fail(err, "Couldn't enable notifications"); }
};
// On launch: if this device already subscribed, re-send it (iOS can rotate
// subscriptions). Never create one here — that needs the button, otherwise a
// second copy of the app (e.g. a preview) would register a duplicate.
async function syncPush() {
  try {
    if (!("PushManager" in window) || Notification.permission !== "granted") return;
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) return;
    await api("/push/subscribe", { method: "POST", body: JSON.stringify(sub) });
    $("#notify").textContent = "Notifications on ✓";
  } catch { /* leave the button as-is */ }
}

$("#test-push").onclick = async () => {
  try { const r = await api("/push/test", { method: "POST" }); toast(`Test sent to ${r.subs} device${r.subs === 1 ? "" : "s"}`); }
  catch (err) { fail(err, "Couldn't send a test"); }
};

// ---------- stats ----------
$("#range").onclick = (e) => {
  const r = e.target.dataset.r; if (!r) return;
  S.range = r;
  document.querySelectorAll("#range button").forEach((b) => b.classList.toggle("on", b.dataset.r === r));
  loadStats();
};

const charts = {};
function plot(id, data, series, opts = {}) {
  const el = $(id);
  charts[id]?.destroy();
  const fg = css("--muted"), grid = css("--line");
  const axis = { stroke: fg, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid } };
  charts[id] = new uPlot({
    width: el.clientWidth || 320, height: 180, legend: { show: true, live: false },
    scales: { x: { time: true } }, axes: [axis, { ...axis, size: 44 }],
    series: [{}, ...series], ...opts,
  }, data, el);
}

async function loadStats() {
  const my = nav;
  try { S.stats = await api(`/stats?range=${S.range}`); }
  catch { if (S.route[0] === "signal") $("#health").innerHTML = `<p class="empty">Can't load signal stats — the bridge isn't reachable.</p>`; return; }
  if (my !== nav) return;
  const { samples, packets, neighbours, hops, health, hourly } = S.stats;
  const acc = css("--accent"), link = css("--link"), ok = css("--ok");

  if (S.route[0] === "signal") {
    const vc = { solid: "q-good", good: "q-good", ok: "q-ok", marginal: "q-ok", poor: "q-bad", bad: "q-bad" }[health.verdict] || "q-none";
    const b = health.best;
    const grade = (cls, word) => `<span class="chip ${cls}">${word}</span>`;
    const errG = health.err_pct == null ? "" : health.err_pct < 20 ? grade("q-good", "good") : health.err_pct < 40 ? grade("q-ok", "ok") : grade("q-bad", "bad");
    const airG = (health.tx_pct_last_hour ?? 0) < 5 ? grade("q-good", "good") : (health.tx_pct_last_hour < 10 ? grade("q-ok", "near limit") : grade("q-bad", "over limit"));
    const snrG = b ? grade(qClass(b.snr_avg), qWord(b.snr_avg)) : "";
    $("#health").innerHTML = `<div class="health">
      <div class="verdict ${vc}">${esc(health.verdict || "–")}</div>
      <div>Best repeater: <b>${esc(b?.name || "–")}</b> ${b ? `<b>${b.snr_avg} dB</b> ${snrG}` : ""}<br><small>${SNR_SCALE}</small></div>
      <div>Packets lost: <b>${health.err_pct ?? "–"}%</b> ${errG}<br><small>lower is better · under 20% good</small></div>
      <div>Noise floor: <b>${health.noise_floor ?? "–"} dBm</b><br><small>compare with your own history (some boards read high)</small></div>
      <div>Your airtime: <b>${health.tx_pct_last_hour ?? 0}%</b> ${airG}<br><small>duty-cycle limit 10%</small></div>
      <div class="hint" style="grid-column:1/-1">Judged on the last hour. Charts below show the selected range.</div></div>`;
    const tsx = (rows) => rows.map((r) => r.ts / 1000);
    plot("#c-trend", [tsx(health.trend), health.trend.map((t) => t.snr)], [{ label: `${b?.name || "repeater"} SNR dB`, stroke: acc, width: 2 }]);
    plot("#c-err", [tsx(hourly), hourly.map((h) => h.err_pct)], [{ label: "lost %", stroke: acc, width: 2, fill: acc + "22" }]);
    plot("#c-air", [tsx(hourly), hourly.map((h) => h.tx_pct), hourly.map(() => 10), hourly.map((h) => h.pkts_per_min)], [
      { label: "your TX %", stroke: link, width: 2 },
      { label: "limit", stroke: acc, width: 1, dash: [4, 4] },
      { label: "mesh pkts/min", stroke: ok, width: 1, scale: "ppm" },
    ], { axes: [{ stroke: css("--muted"), grid: { stroke: css("--line") } }, { stroke: css("--muted"), size: 40 },
      { side: 1, scale: "ppm", stroke: css("--muted"), grid: { show: false }, size: 40 }] });
    const pk = [...packets].reverse();
    plot("#c-pkts", [pk.map((p) => p.ts / 1000), pk.map((p) => p.snr), pk.map((p) => p.rssi)], [
      { label: "SNR dB", stroke: acc, width: 0, points: { show: true, size: 4, fill: acc } },
      { label: "RSSI dBm", stroke: link, width: 0, points: { show: true, size: 4, fill: link }, scale: "rssi" },
    ], { axes: [{ stroke: css("--muted"), grid: { stroke: css("--line") } }, { stroke: css("--muted"), size: 40 },
      { side: 1, scale: "rssi", stroke: css("--muted"), grid: { show: false }, size: 44 }] });
    plot("#c-noise", [samples.map((s) => s.ts / 1000), samples.map((s) => s.radio?.noise_floor ?? null)],
      [{ label: "Noise dBm", stroke: acc, width: 1.5 }]);
    $("#neigh tbody").innerHTML = neighbours.map((n) => `<tr><td>${esc(n.name)}</td><td>${n.count}</td>
      <td>${n.snr_avg ?? ""}</td><td>${n.rssi_avg ?? ""}</td><td>${ago(n.last)}</td></tr>`).join("")
      || `<tr><td colspan="5" class="empty">No packets in this range.</td></tr>`;
    const max = Math.max(1, ...hops.map((h) => h.count));
    $("#hops").innerHTML = hops.map((h) => `<div style="height:${(h.count / max) * 100}%" title="${h.count} packets"><span>${h.hops}</span></div>`).join("");
  } else if (S.route[0] === "radio") {
    plot("#c-bat", [samples.map((s) => s.ts / 1000), samples.map((s) => (s.battery?.level ? lipoPct(s.battery.level / 1000) : null))],
      [{ label: "Battery %", stroke: ok, width: 1.5 }], { scales: { x: { time: true }, y: { range: [0, 100] } } });
  }
}


// ---------- thread info (channel key / delete, contact link) ----------
$("#info").onclick = async () => {
  const [kind, peer] = S.route;
  if (kind === "dm") { const c = S.contacts.find((x) => x.prefix === peer); if (c) location.hash = `#/contact/${c.public_key}`; return; }
  const name = chanName(peer);
  let keyLine = "";
  if (!name.startsWith("#")) {
    try { const s = await api(`/channels/${peer}/secret`); keyLine = `<dl class="kv"><dt>Key</dt><dd><code class="sel">${esc(s.secret)}</code></dd></dl>`; } catch { /* offline */ }
  }
  showSheet(`<h3 id="sheet-title" tabindex="-1" autofocus>${esc(name)}</h3>
    <p class="hint">${name.startsWith("#") ? "Public hashtag channel — anyone who adds the same name can read and post." : name === "Public" ? "The default public channel everyone shares." : "Private channel — only people with its key can read it."}</p>
    ${keyLine}<p class="hint">Radio slot ${esc(peer)}</p>
    <div class="actions"><button type="button" class="btn danger" id="chan-del">Remove channel</button></div>`);
  $("#chan-del").onclick = async () => {
    if (!await confirmSheet(`Remove ${name}?`, "It's removed from your radio. You can add it again later.", "Remove", true)) return;
    try { await api(`/channels/${peer}`, { method: "DELETE" }); await refreshState(); toast(`${name} removed`); location.hash = "#/chats"; }
    catch (err) { fail(err, "Couldn't remove"); }
  };
};

// Inline form instead of prompt(): iOS blocks window.prompt() in an installed PWA.
const addForm = $("#add-chan-form");
const isPrivate = (name) => !name.startsWith("#") && name.trim().toLowerCase() !== "public" && name.trim() !== "";
function syncAddKey() {
  const name = addForm.name.value.trim();
  $("#add-chan-key").hidden = !isPrivate(name);
}
$("#add-chan").onclick = () => {
  addForm.hidden = !addForm.hidden;
  $("#add-chan").setAttribute("aria-expanded", String(!addForm.hidden));
  if (!addForm.hidden) { addForm.reset(); syncAddKey(); addForm.name.focus(); }
};
$("#add-chan-cancel").onclick = () => { addForm.hidden = true; $("#add-chan").setAttribute("aria-expanded", "false"); };
addForm.name.addEventListener("input", syncAddKey);
addForm.onsubmit = async (e) => {
  e.preventDefault();
  let name = addForm.name.value.trim();
  let secret = isPrivate(name) ? (addForm.secret.value.trim() || null) : null;
  // Accept a pasted meshcore://channel/add?name=..&secret=.. link in the name box.
  const uri = name.match(/^meshcore:\/\/channel\/add\?(.+)$/i);
  if (uri) {
    const q = new URLSearchParams(uri[1]);
    name = (q.get("name") || "").trim();
    secret = q.get("secret") || null;
  }
  if (!name) return;
  try {
    const r = await api("/channels", { method: "POST", body: JSON.stringify({ name, secret }) });
    addForm.hidden = true;
    await refreshState();
    toast(`Joined ${r.name || name}`);
    location.hash = `#/chan/${r.idx}`;   // its key is shown in the channel's details
  } catch (err) { fail(err, "Couldn't add channel"); }
};

// ---------- contacts ----------
let ctype = "", cquery = "";
$("#ctype").onclick = (e) => {
  if (e.target.dataset.t === undefined) return;
  ctype = e.target.dataset.t;
  document.querySelectorAll("#ctype button").forEach((b) => b.classList.toggle("on", b.dataset.t === ctype));
  renderContactList();
};
$("#contact-search").addEventListener("input", debounce(() => {
  cquery = $("#contact-search").value.trim().toLowerCase(); renderContactList();
}, 120));

// Filter (type / ★ favourites) + search by name or key, over the contacts we already have.
function renderContactList() {
  const q = cquery;
  const list = S.contacts.filter((c) => (!ctype || (ctype === "fav" ? c.favourite : String(c.type) === ctype))
      && (!q || c.name.toLowerCase().includes(q) || c.public_key.startsWith(q)))
    .sort((a, b) => (b.favourite - a.favourite) || (b.last_advert || 0) - (a.last_advert || 0) || a.name.localeCompare(b.name));
  $("#contact-list").innerHTML = list.map((c) => `<a href="#/contact/${c.public_key}"${S.route[0] === "contact" && S.route[1] === c.public_key ? ` aria-current="true"` : ""}>
    ${c.favourite ? `<span class="badge fav">★${presenceDot(c)}</span>` : `<span class="badge tint"${tint(c.name)}>${esc(initial(c.name))}${presenceDot(c)}</span>`}
    <span class="meta"><div class="name">${esc(c.name)} <span class="tag">${TYPES[c.type] || c.type}</span></div>
    <div class="last">${routeText(c)}</div></span>
    <span class="when">${presence(c) ? ago(c.heard[0] * 1000) : c.last_advert ? ago(c.last_advert * 1000) : ""}</span></a>`).join("")
    || `<p class="empty">${q ? `No contacts match "${esc(q)}".`
      : ctype === "fav" ? "No favourites yet. Open a contact and tap ☆ Favourite to keep it here (favourites are never auto-removed)."
      : ctype ? "None of this type." : "No contacts yet. People you hear appear under Nearby."}</p>`;
}

let nearbyHours = 1;
$("#nearby-range").onclick = (e) => {
  const h = e.target.dataset.h; if (!h) return;
  nearbyHours = +h;
  document.querySelectorAll("#nearby-range button").forEach((x) => x.classList.toggle("on", x.dataset.h === h));
  loadContacts();
};

async function loadContacts() {
  let contacts, pending, near, deliv, ver;
  try {
    [contacts, pending, near, deliv, ver] = await Promise.all([api("/contacts"), api("/pending"),
      api(`/nearby?hours=${nearbyHours}`), api("/delivery"), api("/trust")]);
  } catch {
    if (!S.contacts.length) $("#contact-list").innerHTML = `<p class="empty">Can't reach the bridge right now.</p>`;
    return;
  }
  S.contacts = contacts;
  const max = S.state?.device?.max_contacts;
  $("#contact-count").textContent = `${contacts.length}${max ? " / " + max : ""}`;
  renderContactList();

  $("#nearby").innerHTML = near.map((n) => `<label>
    <span class="badge tint"${tint(n.name || n.pubkey)}>${esc(initial(n.name || n.pubkey))}</span>
    <span class="meta"><div class="name">${esc(n.name || n.pubkey.slice(0, 8))} <span class="tag">${TYPES[n.type] || ""}</span> ${hashBadge(n.hash_size)} ${callChip(n.name)}</div>
    <div class="last"><span class="chip ${qClass(n.snr)}">${n.snr ?? "?"} dB</span> <span class="chip chip-hops">${hopsText(n.path_len)}</span>
      ${n.last_hop && n.last_hop !== "direct" ? "via " + esc(n.last_hop) : ""} · heard ${n.count}×</div></span>
    <span class="when">${ago(n.last_ts)}${n.is_contact ? "" : ` <button class="btn sm" data-addkey="${n.pubkey}">Add</button>`}</span></label>`).join("")
    || `<p class="empty">Nothing heard in the last ${nearbyHours === 1 ? "hour" : nearbyHours + " hours"}. Nodes advertise every few hours.</p>`;
  $("#nearby-note").textContent = near.length ? `${near.length}` : "";

  $("#pending-wrap").hidden = !pending.length;
  $("#pending").innerHTML = pending.map((p) => `<label><span class="badge tint"${tint(p.name)}>${esc(initial(p.name))}</span>
    <span class="meta"><div class="name">${esc(p.name)}</div><div class="last">${TYPES[p.type] || ""}</div></span>
    <button class="btn sm primary" data-add="${p.public_key}">Add</button></label>`).join("");

  $("#verified-list").innerHTML = ver.map((v) => `<label><span class="badge ok">${v.pinned_key ? "🔏" : "✓"}</span>
    <span class="meta"><div class="name">${esc(v.sender)}</div><div class="last">${v.pinned_key
      ? "pinned key " + esc(v.pinned_key.slice(0, 12)) + "…"
      : "sends via " + v.origins.map((o) => esc(o.name)).join(", ")}</div></span>
    <button class="btn sm" data-forget="${esc(v.sender)}">Remove</button></label>`).join("")
    || `<p class="hint">None yet. Open a signed channel message and pin the sender's key, or tap "✓ This is really …" on an unsigned one.</p>`;
  $("#delivery tbody").innerHTML = deliv.map((d) => `<tr><td>${esc(d.name)}</td><td>${d.sent}</td><td>${d.delivered || 0}</td>
    <td>${d.failed || 0}${d.failed_but_repeated ? ` <small>(${d.failed_but_repeated} reached mesh)</small>` : ""}</td>
    <td>${d.avg_rtt_ms ? (d.avg_rtt_ms / 1000).toFixed(1) + "s" : "–"}</td></tr>`).join("")
    || `<tr><td colspan="5" class="empty">No direct messages sent yet.</td></tr>`;
}
const loadContactsSoon = debounce(() => { if (S.route[0] === "contacts" || (isSplit() && S.route[0] === "contact")) loadContacts(); }, 500);

$("#verified-list").addEventListener("click", async (e) => {
  const s = e.target.dataset.forget; if (!s) return;
  e.preventDefault();
  if (!await confirmSheet(`Stop verifying ${s}?`, "Their name won't be checked against where they send from any more.", "Stop verifying")) return;
  try { await api("/trust/forget", { method: "POST", body: JSON.stringify({ sender: s }) }); loadContacts(); }
  catch (err) { fail(err, "Couldn't remove"); }
});

$("#nearby").addEventListener("click", async (e) => {
  const key = e.target.dataset.addkey; if (!key) return;
  e.preventDefault();
  try { await api(`/pending/${key}`, { method: "POST" }); toast("Added to contacts"); loadContacts(); }
  catch { toast("Can't add yet — wait for its next advert, or add it from a meshcore:// link.", "err"); }
});

$("#pending").addEventListener("click", async (e) => {
  const key = e.target.dataset.add; if (!key) return;
  e.preventDefault();
  try { await api(`/pending/${key}`, { method: "POST" }); toast("Added to contacts"); loadContacts(); refreshState().catch(() => {}); }
  catch (err) { fail(err, "Couldn't add"); }
});

$("#import").onsubmit = async (e) => {
  e.preventDefault();
  const uri = $("#uri").value.trim(); if (!uri) return;
  if (await handleMeshUri(uri)) $("#uri").value = "";
};

async function shareUri(uri, title) {
  if (navigator.share) { try { await navigator.share({ title, text: uri }); return; } catch { /* cancelled */ } }
  try { await navigator.clipboard.writeText(uri); toast("Link copied"); }
  catch {   // no clipboard (e.g. plain http): show it to copy by hand — prompt() is blocked in home-screen apps
    showSheet(`<h3 id="sheet-title" tabindex="-1" autofocus>${esc(title)}</h3><p class="hint">Copy this link and send it to whoever should add you:</p>
      <textarea class="sel-box" readonly rows="4">${esc(uri)}</textarea>`);
    $("#sheet .sel-box").select();
  }
}
$("#my-uri").onclick = async () => {
  try { const r = await api("/self/uri"); shareUri(r.uri, "My MeshCore contact"); } catch (err) { fail(err, "Couldn't get your contact link"); }
};

// ---------- QR codes: show yours, scan theirs ----------
const scripts = {};
const loadScript = (src) => scripts[src] ||= new Promise((ok, bad) => {
  const s = document.createElement("script"); s.src = src; s.onload = ok;
  s.onerror = () => { delete scripts[src]; bad(new Error("couldn't load " + src)); };
  document.head.appendChild(s);
});

async function showQr(title, uri, sub = "") {
  await loadScript("vendor/qrcode.js");
  const q = qrcode(0, "M"); q.addData(uri); q.make();
  showSheet(`<h3 id="sheet-title" tabindex="-1" autofocus>${esc(title)}</h3>
    ${sub ? `<p class="hint">${esc(sub)}</p>` : ""}
    <div class="qr-box">${q.createSvgTag({ cellSize: 6, margin: 3, scalable: true })}</div>
    <div class="actions"><button type="button" class="btn primary" id="qr-share">Share link</button></div>`);
  $("#qr-share").onclick = () => shareUri(uri, title);
}

// Advert-format contact link (meshcore://<hex>): pull out the name + type for the confirm text.
function decodeAdvertUri(hex) {
  try {
    const b = Uint8Array.from(hex.match(/../g).map((h) => parseInt(h, 16)));
    let o = 2 + (b[1] & 0x3f);                  // header, path length, path
    const key = [...b.slice(o, o + 32)].map((x) => x.toString(16).padStart(2, "0")).join("");
    o += 32 + 4 + 64;                           // pubkey, timestamp, signature
    const flags = b[o++];
    if (flags & 0x10) o += 8; if (flags & 0x20) o += 2; if (flags & 0x40) o += 2;
    const name = flags & 0x80 ? new TextDecoder().decode(b.slice(o)).replace(/\0.*$/, "") : "";
    return { key, name, type: flags & 0x0f };
  } catch { return null; }
}

// One handler for every meshcore:// link, typed, pasted or scanned.
async function handleMeshUri(raw) {
  const uri = raw.trim();
  const m = uri.match(/^meshcore:\/\/(contact|channel)\/add\?(.+)$/i);
  try {
    if (m && m[1].toLowerCase() === "channel") {
      const q = new URLSearchParams(m[2]), name = (q.get("name") || "").trim();
      if (!name) throw new Error("the link has no channel name");
      if (!await confirmSheet(`Join ${name}?`, "It's added to your radio's channels.", "Join")) return false;
      const r = await api("/channels", { method: "POST", body: JSON.stringify({ name, secret: q.get("secret") || null }) });
      await refreshState(); toast(`Joined ${r.name || name}`); location.hash = `#/chan/${r.idx}`;
      return true;
    }
    if (m) {
      const q = new URLSearchParams(m[2]);
      const card = { public_key: (q.get("public_key") || "").toLowerCase(), type: +(q.get("type") || 1), name: q.get("name") || "" };
      if (!/^[0-9a-f]{64}$/.test(card.public_key)) throw new Error("the link has no valid public key");
      if (!await confirmSheet(`Add ${card.name || "this contact"}?`, `${TYPES[card.type] || "contact"} · ${card.public_key.slice(0, 8)}…`, "Add")) return false;
      await api("/contacts/card", { method: "POST", body: JSON.stringify({ ...card, name: card.name || card.public_key.slice(0, 8) }) });
    } else {
      const hex = uri.replace(/^meshcore:\/\//i, "").split("?")[0];
      if (!/^([0-9a-f]{2})+$/i.test(hex)) throw new Error("that isn't a MeshCore contact or channel link");
      const info = decodeAdvertUri(hex);
      if (info?.key && info.key === S.state?.public_key) throw new Error("that's your own contact");
      if (!await confirmSheet(`Add ${info?.name || "this contact"}?`, info ? `${TYPES[info.type] || "contact"} · ${info.key.slice(0, 8)}…` : "", "Add")) return false;
      await api("/contacts/import", { method: "POST", body: JSON.stringify({ uri }) });
    }
    toast("Contact added"); S.contacts = await api("/contacts").catch(() => S.contacts);
    if (S.route[0] === "contacts") loadContacts();
    return true;
  } catch (err) { fail(err, "Couldn't add"); return false; }
}

async function scanQr() {
  if (!navigator.mediaDevices?.getUserMedia) return toast("This browser can't use the camera", "err");
  showSheet(`<h3 id="sheet-title" tabindex="-1" autofocus>Scan a MeshCore QR code</h3>
    <div class="scan-box"><video id="scan-video" playsinline muted></video><div class="scan-frame" aria-hidden="true"></div></div>
    <p class="hint" id="scan-note">Point the camera at a contact or channel QR code.</p>`);
  const video = $("#scan-video"), sheet = $("#sheet");
  let stream, stopped = false;
  const stop = () => { stopped = true; stream?.getTracks().forEach((t) => t.stop()); };
  sheet.addEventListener("close", stop, { once: true });
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
  } catch (err) {
    $("#scan-note").textContent = err.name === "NotAllowedError"
      ? "Camera access was blocked. Allow it for this site in your browser settings, then try again."
      : `Can't open the camera (${err.message}).`;
    return;
  }
  if (stopped) return stop();
  video.srcObject = stream; await video.play().catch(() => {});
  let detect;
  if ("BarcodeDetector" in window && (await BarcodeDetector.getSupportedFormats?.() || []).includes("qr_code")) {
    const bd = new BarcodeDetector({ formats: ["qr_code"] });
    detect = async () => (await bd.detect(video))[0]?.rawValue;
  } else {
    await loadScript("vendor/jsQR.js");
    const cv = document.createElement("canvas"), cx = cv.getContext("2d", { willReadFrequently: true });
    detect = async () => {
      const w = video.videoWidth, h = video.videoHeight; if (!w) return null;
      const k = Math.min(1, 640 / Math.max(w, h)); cv.width = w * k; cv.height = h * k;
      cx.drawImage(video, 0, 0, cv.width, cv.height);
      return jsQR(cx.getImageData(0, 0, cv.width, cv.height).data, cv.width, cv.height, { inversionAttempts: "dontInvert" })?.data;
    };
  }
  let lastBad = "";
  while (!stopped) {
    const val = await detect().catch(() => null);
    if (val && /^meshcore:\/\//i.test(val)) {
      stop(); sheet.close();
      if (navigator.vibrate) { try { navigator.vibrate(60); } catch { /* unsupported */ } }
      handleMeshUri(val);
      return;
    }
    if (val && val !== lastBad) { lastBad = val; $("#scan-note").textContent = "That QR code isn't a MeshCore contact or channel."; }
    await new Promise((r) => setTimeout(r, 150));
  }
}

$("#scan-qr").onclick = () => scanQr();
$("#my-qr").onclick = async () => {
  try { const r = await api("/self/uri"); showQr(S.state?.name || "My contact", r.uri, "Let someone scan this to add you."); }
  catch (err) { fail(err, "Couldn't get your contact"); }
};

// ---------- amateur callsigns in node names ----------
// UK callsigns only: G/M (+ regional letter), 2 + letter (Intermediate), GB (special event).
const CALL_RE = /(?<![A-Z0-9])((?:G[DIJMUW]?|M[DIJMUW]?|2[DEIJMUW]|GB)[0-9][A-Z]{2,4})(?![A-Z0-9])/;
const callsignOf = (name) => (String(name || "").toUpperCase().match(CALL_RE) || [])[1] || null;
const REGION = { "": "England", E: "England", W: "Wales", M: "Scotland", I: "Northern Ireland", D: "Isle of Man", J: "Jersey", U: "Guernsey" };
// When each UK callsign series was issued (g3lrs.org.uk). [from, to|null, licence class today]
const PRE_WAR = [1920, 1939, "Full"];
const ERA = {
  G2: PRE_WAR, G3: [1946, 1971, "Full"], G4: [1971, 1985, "Full"], G5: [1966, 1981, "Full"],
  G6: [1981, 1983, "Full"], G8: [1964, 1981, "Full"], G1: [1983, 1988, "Full"], G0: [1986, 1996, "Full"],
  G7: [1989, 1996, "Full"], M0: [1996, null, "Full"], M1: [1996, 2003, "Full"], M5: [1999, 2001, "Full"],
  M3: [2002, 2008, "Foundation"], M6: [2008, 2018, "Foundation"], M7: [2018, null, "Foundation"],
  M8: [2025, null, "Intermediate"], M9: [2025, null, "Intermediate"],
  20: [1991, 2025, "Intermediate"], 21: [1991, 2001, "Intermediate"],
};
function callsignInfo(call) {
  const m = call.match(/^(GB|[GM2])([A-Z]?)([0-9])([A-Z]+)$/);
  if (!m) return { call };
  const [, p, r, d, suf] = m;
  if (p === "GB") return { call, kind: "Special event or club station" };
  const base = p === "2" ? `2${d}` : p + d;
  let era = ERA[base];
  if (p === "G" && "234568".includes(d) && suf.length === 2) era = PRE_WAR;   // two-letter pre-war calls
  const since = !era ? null : era[1] == null ? `${era[0]} or later` : `~${era[0]}–${era[1]}`;
  return { call, region: REGION[r] || "England", cls: era?.[2] || null, since };
}
const callChip = (name) => { const c = callsignOf(name); return c ? `<button type="button" class="chip callsign" data-call="${c}" aria-label="Callsign ${c} profile">📻 ${c}</button>` : ""; };

async function showCallsign(call) {
  const i = callsignInfo(call);
  const facts = {
    "Ham since": i.since ? `${i.since}<small class="muted"> · from when this callsign series was issued</small>` : null,
    Licence: i.cls ? `${i.cls}` : i.kind || null,
    Region: i.region || null,
  };
  const kv = (o) => Object.entries(o).filter(([, v]) => v).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("");
  showSheet(`<h3 id="sheet-title" tabindex="-1" autofocus>📻 ${esc(call)}</h3>
    <dl class="kv">${kv(facts)}</dl>
    <div id="cs-qrz" class="cs-card"><p class="hint">Looking up QRZ…</p></div>
    <div class="actions"><a class="btn primary" href="https://www.qrz.com/db/${encodeURIComponent(call)}" target="_blank" rel="noopener">Open on QRZ.com</a></div>`);
  let r;
  try { r = await api(`/callsign/${encodeURIComponent(call)}`); }
  catch { r = { error: "the bridge isn't reachable" }; }
  const el = $("#cs-qrz");
  if (!el || !$("#sheet").open) return;            // sheet closed or replaced meanwhile
  const p = r.profile;
  if (p) {
    const name = [p.fname, p.name].filter(Boolean).join(" ");
    el.innerHTML = `${p.image ? `<img class="cs-photo" src="${esc(p.image)}" alt="" referrerpolicy="no-referrer" loading="lazy">` : ""}
      <dl class="kv">${kv({ Name: esc(name || p.nickname || ""), Town: esc([p.addr2, p.state].filter(Boolean).join(", ")),
        Country: esc(p.country || ""), Grid: esc(p.grid || ""), "Licensed (QRZ)": esc(p.efdate || "") })}</dl>`;
  } else if (r.error) el.innerHTML = `<p class="hint">Couldn't look up QRZ: ${esc(r.error)}</p>`;
  else if (r.configured === false) el.innerHTML = `<p class="hint">Add a QRZ.com login to the bridge (<code>QRZ_USERNAME</code> / <code>QRZ_PASSWORD</code>) to see names here.</p>`;
  else el.innerHTML = `<p class="hint">No QRZ profile for ${esc(call)}.</p>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-call]"); if (!b) return;
  e.preventDefault(); e.stopPropagation();
  showCallsign(b.dataset.call);
}, true);

// ---------- one contact ----------
function logConsole(line) { const c = $("#console"); c.textContent += line + "\n"; c.scrollTop = 1e9; }
// Diagnostic results as a small card: a title, then label/value rows (or a line of text).
function showOut(title, body, tone = "") {
  const o = $("#c-out"); o.hidden = false; o.className = "result " + tone;
  const rows = typeof body === "string" ? `<p>${esc(body)}</p>`
    : `<dl class="kv">${Object.entries(body).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${typeof v === "object" ? esc(JSON.stringify(v)) : v}</dd>`).join("")}</dl>`;
  o.innerHTML = `<div class="result-title">${esc(title)}</div>${rows}`;
  o.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// Rough single-cell LiPo resting voltage -> % (non-linear, load-dependent — a ballpark).
function lipoPct(v) {
  const t = [[3.27, 0], [3.61, 5], [3.69, 10], [3.73, 20], [3.75, 25], [3.77, 30],
    [3.79, 40], [3.82, 50], [3.87, 60], [3.92, 70], [3.98, 80], [4.06, 90], [4.20, 100]];
  if (v <= t[0][0]) return 0;
  if (v >= t[t.length - 1][0]) return 100;
  for (let i = 1; i < t.length; i++) {
    if (v < t[i][0]) {
      const [v0, p0] = t[i - 1], [v1, p1] = t[i];
      return Math.round(p0 + (p1 - p0) * (v - v0) / (v1 - v0));
    }
  }
  return 100;
}

function fmtTelemetry(t) {
  if (!Array.isArray(t) || !t.length) return "No telemetry reported.";
  const out = {};
  for (const e of t) {
    const k = e.type === "voltage" ? "Battery" : e.type === "temperature" ? "Temperature"
      : e.type[0].toUpperCase() + e.type.slice(1) + (e.channel > 1 ? ` (${e.channel})` : "");
    out[k] = e.type === "voltage" ? `${e.value < 4.35 ? `~${lipoPct(e.value)}% · ` : ""}${e.value} V`
      : e.type === "temperature" ? `${e.value} °C` : esc(e.value);
  }
  return out;
}

// Repeater/room status: the raw counters, translated into what helps delivery debugging.
function fmtStatus(r) {
  const v = r.bat ? r.bat / 1000 : null, up = r.uptime || 0;
  const pctOf = (x) => up ? ` (${((x / up) * 100).toFixed(1)}%)` : "";
  const dur = (s) => s >= 86400 ? `${(s / 86400).toFixed(1)} days` : `${(s / 3600).toFixed(1)} h`;
  const out = {
    Battery: v ? `${lipoPct(v)}% · ${v.toFixed(2)} V` : null,
    Uptime: up ? dur(up) : null,
    "Hears you at": r.last_snr != null ? `<span class="chip ${snrGrade(r.last_snr).cls}">${r.last_snr} dB</span> <small>last packet it received</small>` : null,
    "Noise floor": r.noise_floor != null ? `${r.noise_floor} dBm` : null,
    "Waiting to send": r.tx_queue_len != null ? `${r.tx_queue_len} packet${r.tx_queue_len === 1 ? "" : "s"}` : null,
    "Dropped (queue full)": r.full_evts != null ? `${r.full_evts}${r.full_evts ? " <small>— it's been too busy to forward everything</small>" : ""}` : null,
    Relayed: r.sent_flood != null ? `${r.sent_flood} flood · ${r.sent_direct} direct` : null,
    Received: r.recv_flood != null ? `${r.recv_flood} flood · ${r.recv_direct} direct` : null,
    "Duplicates ignored": r.flood_dups != null ? `${r.flood_dups} flood · ${r.direct_dups} direct` : null,
    "Corrupt packets": r.recv_errors != null ? String(r.recv_errors) : null,
    "Transmit airtime": r.airtime != null ? `${dur(r.airtime)}${pctOf(r.airtime)}` : null,
    "Receive airtime": r.rx_airtime ? `${dur(r.rx_airtime)}${pctOf(r.rx_airtime)}` : null,
  };
  return Object.fromEntries(Object.entries(out).filter(([, x]) => x != null));
}

// ---------- repeater battery watch ----------
const bwTone = (r) => !r || r.pct == null ? "" : r.pct < 20 ? "bad" : r.pct < 40 ? "warn" : "ok";
function bwCard(w, withLink = false) {
  const r = w.reading;
  const when = r ? (r.error ? `last check ${ago(r.ts)} failed: ${r.error}${r.good_ts ? ` · showing reading from ${ago(r.good_ts)}` : ""}` : `checked ${ago(r.ts)}`) : "first check within a few minutes";
  return `<div class="bw-card ${bwTone(r)}">
    <div class="bw-top"><span class="name">${withLink ? `<a href="#/contact/${esc(w.public_key)}">${esc(w.name)}</a>` : "Battery watch"}</span>
      <span class="bw-pct">${r?.pct != null ? `${r.pct}%` : "–"}</span></div>
    <div class="bw-sub">${r?.v ? `${r.v.toFixed(2)} V · ` : ""}alert below ${w.below}%${w.has_password ? "" : " · no login"}</div>
    <div class="bw-sub">${esc(when)}</div>
  </div>`;
}

async function renderBatteryWatch(c) {
  const el = $("#c-bw"), my = nav;
  if (c.type !== 2 && c.type !== 3) { el.innerHTML = ""; return; }
  let list;
  try { list = await api("/battery-watch"); } catch { el.innerHTML = ""; return; }
  if (my !== nav) return;
  const w = list.find((x) => x.public_key === c.public_key);
  if (w) {
    el.innerHTML = `${bwCard(w)}<div class="row-actions"><button class="btn sm" id="bw-check">Check now</button><button class="btn sm" id="bw-off">Stop watching</button></div>`;
    $("#bw-check").onclick = act($("#bw-check"), async () => {
      const r = await apiOTA(`/battery-watch/${c.prefix}/check`, { method: "POST" }, 1);
      if (my === nav) renderBatteryWatch(c);
      if (r.error) throw new Error(r.error);
      return r;
    }, (r) => `${c.name}: ${r.pct}% (${r.v.toFixed(2)} V)`);
    $("#bw-off").onclick = act($("#bw-off"), async () => {
      await api(`/battery-watch/${c.prefix}`, { method: "POST", body: JSON.stringify({ on: false }) });
      if (my === nav) renderBatteryWatch(c);
    }, () => "Stopped watching");
    return;
  }
  el.innerHTML = `<details class="fold"><summary>Battery watch</summary>
    <p class="hint">Checks ${esc(c.name)}'s battery every 3 hours and notifies you when it runs low, so you can warn its owner. Most repeaters need the guest password first.</p>
    <form id="bw-form" class="form">
      <label>Guest password <input id="bw-pw" type="password" placeholder="Blank = log in with no password" autocomplete="off"></label>
      <label class="check"><input id="bw-nologin" type="checkbox"> Don't log in (it answers anyone)</label>
      <label>Alert below (%) <input id="bw-below" type="number" min="5" max="90" value="30"></label>
      <button class="btn primary">Watch battery</button>
    </form></details>`;
  $("#bw-form").onsubmit = async (e) => {
    e.preventDefault();
    const body = { on: true, below: +$("#bw-below").value || 30, password: $("#bw-nologin").checked ? null : $("#bw-pw").value };
    try {
      await api(`/battery-watch/${c.prefix}`, { method: "POST", body: JSON.stringify(body) });
      toast(`Watching ${c.name}'s battery — also made it a favourite so it's never auto-removed`);
      S.contacts = await api("/contacts").catch(() => S.contacts);
      if (my === nav) openContact(c.public_key);
    } catch (err) { fail(err, "Couldn't start watching"); }
  };
}

async function renderBatteryList() {
  try {
    const list = await api("/battery-watch");
    $("#bw-list").innerHTML = list.length ? list.map((w) => bwCard(w, true)).join("")
      : `<p class="hint">None yet. Open a repeater in Contacts and turn on <b>Battery watch</b>.</p>`;
  } catch { $("#bw-list").innerHTML = `<p class="hint">Can't load — the bridge isn't reachable.</p>`; }
}
$("#notify-offline").onchange = async (e) => {
  try { await api("/notify-offline", { method: "POST", body: JSON.stringify({ on: e.target.checked }) });
    if (S.state) S.state.notify_offline = e.target.checked; toast(e.target.checked ? "Offline alerts on" : "Offline alerts off"); }
  catch (err) { e.target.checked = !e.target.checked; fail(err, "Couldn't save"); }
};
$("#notify-back").onchange = async (e) => {
  try { await api("/notify-back", { method: "POST", body: JSON.stringify({ on: e.target.checked }) });
    if (S.state) S.state.notify_back = e.target.checked; toast(e.target.checked ? "Back-in-range alerts on" : "Back-in-range alerts off"); }
  catch (err) { e.target.checked = !e.target.checked; fail(err, "Couldn't save"); }
};

async function openContact(key) {
  const my = nav;
  $("#v-contact").hidden = false;
  if (!S.contacts.length) { try { S.contacts = await api("/contacts"); } catch { /* shown below */ } }
  if (my !== nav) return;
  const c = S.contacts.find((x) => x.public_key === key || x.prefix === key);   // alerts link by prefix
  $("#c-out").hidden = true; $("#console").textContent = ""; $("#c-route").innerHTML = "";
  $("#c-route-wrap").open = false;
  if (!c) {
    $("#title").textContent = "Contact";
    $("#c-hero").innerHTML = `<p class="empty">This contact isn't on your radio any more.</p>`;
    $("#c-actions").innerHTML = $("#c-kv").innerHTML = $("#c-diag").innerHTML = "";
    $("#c-admin").hidden = true;
    return;
  }
  $("#title").textContent = c.name;
  const isChat = c.type === 1, isNode = c.type === 2 || c.type === 3 || c.type === 4;
  $("#c-hero").innerHTML = `${c.favourite ? `<span class="badge big fav">★</span>` : `<span class="badge big tint"${tint(c.name)}>${esc(initial(c.name))}</span>`}
    <div><div class="hero-name">${esc(c.name)}</div>
    <div class="hero-sub"><span class="tag">${TYPES[c.type] || c.type}</span> ${hashBadge(c.hash_size)} ${callChip(c.name)}
      <span>${c.path_len < 0 ? "No fixed route — messages flood" : c.path_len === 0 ? "Direct — no repeaters" : `Route: ${c.path_len} hop${c.path_len === 1 ? "" : "s"}`}</span></div></div>`;
  $("#c-kv").innerHTML = Object.entries({
    "Last heard": c.last_advert ? new Date(c.last_advert * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "Not heard directly",
    Location: c.lat ? `${c.lat.toFixed(4)}, ${c.lon.toFixed(4)}` : "Not shared",
    Key: c.public_key.slice(0, 16) + "…",
  }).map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
  $("#c-actions").innerHTML = [
    isChat || c.type === 3 ? `<button class="btn primary" data-a="msg">Message</button>` : "",
    `<button class="btn${c.favourite ? " on" : ""}" data-a="fav" aria-pressed="${!!c.favourite}">${c.favourite ? "★ Favourite" : "☆ Favourite"}</button>`,
    c.lat ? `<button class="btn" data-a="map">Show on map</button>` : "",
    `<button class="btn" data-a="share">Share</button>`,
    `<button class="btn danger" data-a="remove">Remove</button>`,
  ].join("");
  $("#c-diag").innerHTML = [
    `<button class="btn" data-a="discover">Find path</button>`,
    c.type === 2 ? `<button class="btn" data-a="trace">Signal both ways</button>` : "",
    isNode ? `<button class="btn" data-a="status">Status</button>` : "",
    `<button class="btn" data-a="telemetry">Telemetry</button>`,
    c.path_len >= 0 ? `<button class="btn" data-a="reset">Reset route</button>` : "",
  ].join("");
  $("#c-admin").hidden = !isNode;
  renderRoute(c);
  renderBatteryWatch(c);
  const handler = async (e) => {
    const btn = e.target.closest("button[data-a]"); if (!btn) return;
    const a = btn.dataset.a, label = btn.textContent;
    if (a === "msg") { location.hash = `#/dm/${c.prefix}`; return; }
    if (a === "map") { S.focus = c; location.hash = "#/map"; return; }
    if (a === "remove") {
      if (!await confirmSheet(`Remove ${c.name}?`, "They're removed from your radio's contacts. You can add them again later.", "Remove", true)) return;
    }
    btn.disabled = true; btn.textContent = "…"; btn.classList.add("busy");
    try {
      if (a === "fav") {
        const r = await api(`/contacts/${key}/favourite`, { method: "POST", body: JSON.stringify({ on: !c.favourite }) });
        c.favourite = r.favourite;
        S.contacts = await api("/contacts");
        toast(c.favourite ? "Added to favourites — never auto-removed" : "Removed from favourites");
        if (my === nav) { openContact(key); if (isSplit()) loadContacts(); }
        return;
      }
      if (a === "discover") {
        const r = await apiOTA(`/contacts/${key}/discover`, { method: "POST" });
        showOut("Path", r.hops.length ? `You → ${r.hops.map((h) => h.name || h.hash).join(" → ")} → ${c.name}` : `${c.name} hears you directly — no repeaters needed.`, "ok");
      }
      if (a === "reset") { await api(`/contacts/${key}/reset-path`, { method: "POST" }); showOut("Route reset", "The next message floods and the radio learns a fresh route.", "ok"); }
      if (a === "status") {
        const r = await apiOTA(`/contacts/${key}/status`, {}, 2);
        showOut("Status", fmtStatus(r), "ok");
      }
      if (a === "trace") {
        const r = await apiOTA(`/contacts/${key}/trace`, { method: "POST" });
        const up = snrGrade(r.repeater_heard_you_snr), down = snrGrade(r.you_heard_repeater_snr);
        showOut("Signal both ways", {
          [`${c.name} hears you`]: `<span class="chip ${up.cls}">${r.repeater_heard_you_snr} dB</span> ${up.word}`,
          [`You hear ${c.name}`]: `<span class="chip ${down.cls}">${r.you_heard_repeater_snr} dB</span> ${down.word}`,
          Scale: `<small>${SNR_SCALE}</small>`,
        });
      }
      if (a === "telemetry") showOut("Telemetry", fmtTelemetry(await apiOTA(`/contacts/${key}/telemetry`, {}, 2)));
      if (a === "share") { const r = await api(`/contacts/${key}/uri`); showQr(c.name, r.uri, `Let someone scan this to add ${c.name}.`); }
      if (a === "remove") { await api(`/contacts/${key}`, { method: "DELETE" }); toast(`${c.name} removed`); location.hash = "#/contacts"; }
    } catch (err) {
      if (["discover", "trace", "status", "telemetry"].includes(a)) showOut("No answer", `${err.message}. It may be out of range right now — try again in a minute.`, "bad");
      else fail(err);
    } finally { btn.disabled = false; btn.textContent = label; btn.classList.remove("busy"); }
  };
  $("#c-actions").onclick = handler;
  $("#c-diag").onclick = handler;
  $("#login").onsubmit = async (e) => {
    e.preventDefault();
    logConsole("→ logging in (over the air — can take a moment)…");
    try { const r = await apiOTA(`/contacts/${key}/login`, { method: "POST", body: JSON.stringify({ password: $("#pw").value }) });
      logConsole(`← logged in ${r.is_admin ? "as admin" : "as guest"}`); $("#pw").value = ""; }
    catch (err) { logConsole(`✗ ${err.message} — it didn't answer. Try again.`); }
  };
  $("#cli").onsubmit = async (e) => {
    e.preventDefault();
    const cmd = $("#cmd").value.trim(); if (!cmd) return;
    logConsole(`→ ${cmd}`); $("#cmd").value = "";
    try { await api(`/contacts/${key}/cli`, { method: "POST", body: JSON.stringify({ cmd }) }); } // reply arrives via SSE "cli"
    catch (err) { logConsole(`✗ ${err.message}`); }
  };
}

// ---------- placement mode ----------
// Live per-repeater signal while you move the antenna. Passive: built from
// packets the radio already receives (the bridge's last 30 min, then live SSE).
const PL = { samples: [], start: 0, traces: {}, bestEver: null, beep: false, loaded: false, testing: false };
const SPOTS_KEY = "placement-spots";
const loadSpots = () => { try { return JSON.parse(localStorage.getItem(SPOTS_KEY) || "[]"); } catch { return []; } };
const saveSpots = (v) => { try { localStorage.setItem(SPOTS_KEY, JSON.stringify(v)); } catch { toast("Couldn't save spots in this browser", "err"); } };
// SNR −20…+10 dB onto a 0–100% bar
const snrBar = (snr) => Math.max(3, Math.min(100, ((snr + 20) / 30) * 100));
const fmtDb = (v) => v == null ? "–" : `${v > 0 ? "+" : ""}${(+v).toFixed(1)}`;
const mmss = (ms) => { const s = Math.max(0, Math.round(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };

function plAggregate() {
  const by = {};
  for (const x of PL.samples) {
    if (x.ts < PL.start) continue;
    const k = x.hop.slice(0, 2);   // 2-byte path hashes: group with the same repeater's 1-byte form
    const b = by[k] ||= { hop: k, name: (x.name || k).replace(/\?$/, ""), rep: x.rep, snrs: [], last: 0 };
    b.snrs.push(x.snr);
    if (x.ts >= b.last) { b.last = x.ts; b.snr_last = x.snr; }
    if (x.name && !x.name.endsWith("?")) b.name = x.name;   // prefer an unambiguous name
  }
  return Object.values(by).map((b) => ({ ...b, count: b.snrs.length,
    avg: b.snrs.reduce((a, v) => a + v, 0) / b.snrs.length, best: Math.max(...b.snrs) }))
    .sort((a, b) => b.avg - a.avg);
}

async function openPlacement() {
  const my = nav;
  if (!PL.start) PL.start = Date.now() - 3 * 60_000;   // first visit: use the last 3 minutes
  renderPlacement();
  if (PL.loaded) return;
  try {
    const r = await api("/placement?mins=30");
    if (my !== nav) return;
    const seen = new Set(PL.samples.map((x) => `${x.ts}:${x.hop}`));
    PL.samples = [...r.samples.filter((x) => !seen.has(`${x.ts}:${x.hop}`)), ...PL.samples].sort((a, b) => a.ts - b.ts);
    PL.noise = r.noise_floor; PL.loaded = true;
    renderPlacement();
  } catch (err) {
    if (my === nav) $("#pl-list").innerHTML = `<p class="empty">Can't load signal data — the bridge isn't reachable.</p>`;
  }
}

function plRow(n) {
  const g = snrGrade(n.avg), t = PL.traces[n.hop];
  const tr = t?.err ? `<span class="pl-tr muted">no reply</span>`
    : t ? `<span class="pl-tr"><span class="chip ${snrGrade(t.up).cls}" title="${esc(n.name)} hears you">↑ ${fmtDb(t.up)}</span>
        <span class="chip ${snrGrade(t.down).cls}" title="You hear ${esc(n.name)}">↓ ${fmtDb(t.down)}</span></span>` : "";
  return `<div class="pl-row">
    <div class="pl-top"><span class="pl-name">${esc(n.name)}</span><span class="pl-db">${fmtDb(n.avg)} <small>dB</small></span></div>
    <div class="pl-bar ${g.cls}" role="meter" aria-valuemin="-20" aria-valuemax="10" aria-valuenow="${n.avg.toFixed(1)}" aria-label="${esc(n.name)} signal">
      <span style="width:${snrBar(n.avg)}%"></span></div>
    <div class="pl-sub"><span>${g.word} · last ${fmtDb(n.snr_last)} · best ${fmtDb(n.best)} · ${n.count} pkt${n.count === 1 ? "" : "s"} · ${ago(n.last)}</span>${tr}</div>
    ${n.rep ? `<button class="btn sm pl-one" data-hop="${esc(n.hop)}" ${PL.testing ? "disabled" : ""}>Test</button>` : ""}
  </div>`;
}

function renderPlacement() {
  if (S.route[0] !== "placement") return;
  const nodes = plAggregate(), reps = nodes.filter((n) => n.rep), other = nodes.filter((n) => !n.rep);
  const top = reps.find((n) => n.count >= 2) || reps[0];
  const listening = Date.now() - PL.start, pkts = nodes.reduce((a, n) => a + n.count, 0);
  // new best (needs a couple of packets so one lucky frame doesn't count)
  if (top && top.count >= 2 && (PL.bestEver == null || top.avg > PL.bestEver + 0.5)) {
    const first = PL.bestEver == null;
    PL.bestEver = top.avg;
    if (!first) { plBeep(); toast(`New best spot: ${top.name} ${fmtDb(top.avg)} dB`); }
  }
  const g = top ? snrGrade(top.avg) : null;
  $("#pl-head").innerHTML = top
    ? `<div class="pl-k">Strongest link here</div>
       <div class="pl-big ${g.cls}">${fmtDb(top.avg)}<small> dB</small></div>
       <div class="pl-who">${esc(top.name)} · ${g.word}</div>
       <div class="pl-meta">Listening ${mmss(listening)} · ${pkts} packets · ${reps.length} repeater${reps.length === 1 ? "" : "s"}${PL.noise != null ? ` · noise ${PL.noise} dBm` : ""}${PL.bestEver != null ? ` · best so far ${fmtDb(PL.bestEver)}` : ""}</div>`
    : `<div class="pl-k">Listening ${mmss(listening)}</div><div class="pl-big">…</div>
       <div class="pl-who">Waiting for packets — repeaters are usually heard within a minute</div>`;
  $("#pl-since").textContent = `since ${hhmm(PL.start)}`;
  $("#pl-list").innerHTML = reps.length ? reps.map(plRow).join("")
    : `<p class="empty">${PL.loaded ? "No repeaters heard at this spot yet." : "Loading…"}</p>`;
  $("#pl-other-wrap").hidden = !other.length;
  $("#pl-other").innerHTML = other.map(plRow).join("");
  $("#pl-test").disabled = PL.testing || !reps.length;
  $("#pl-save").disabled = !nodes.length;
  renderSpots();
}
const renderPlacementSoon = debounce(renderPlacement, 400);
setInterval(() => { if (S.route[0] === "placement" && document.visibilityState === "visible") renderPlacement(); }, 5000);

let audioCtx;
function plBeep() {
  if (navigator.vibrate) { try { navigator.vibrate([80, 60, 80]); } catch { /* unsupported */ } }
  if (!PL.beep || !audioCtx) return;
  const o = audioCtx.createOscillator(), gn = audioCtx.createGain();
  o.frequency.value = 1320; gn.gain.setValueAtTime(0.15, audioCtx.currentTime);
  gn.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.25);
  o.connect(gn).connect(audioCtx.destination); o.start(); o.stop(audioCtx.currentTime + 0.25);
}
$("#pl-beep").onchange = (e) => {
  PL.beep = e.target.checked;
  // audio must be unlocked from a tap (iOS); play a short blip so you know it works
  if (PL.beep) { try { audioCtx ||= new (window.AudioContext || window.webkitAudioContext)(); audioCtx.resume(); plBeep(); } catch { toast("This browser can't beep", "err"); } }
};
$("#pl-new").onclick = () => { PL.start = Date.now(); PL.traces = {}; renderPlacement(); toast("New spot — give it a minute"); };

async function plTrace(hop) {
  try { const r = await api(`/trace/${hop}`, { method: "POST" });
    PL.traces[hop] = { up: r.repeater_heard_you_snr, down: r.you_heard_repeater_snr, ts: Date.now() }; }
  catch { PL.traces[hop] = { err: true, ts: Date.now() }; }
  renderPlacement();
}
async function plTestAll(hops) {
  if (PL.testing) return;
  PL.testing = true; renderPlacement();
  try {
    for (const [i, hop] of hops.entries()) {
      if (i) await new Promise((r) => setTimeout(r, 1500));   // let the channel clear between traces
      await plTrace(hop);
    }
  } finally { PL.testing = false; renderPlacement(); }
}
$("#pl-test").onclick = () => plTestAll(plAggregate().filter((n) => n.rep).slice(0, 4).map((n) => n.hop));
$("#pl-list").addEventListener("click", (e) => { const h = e.target.closest("[data-hop]")?.dataset.hop; if (h) plTestAll([h]); });

$("#pl-save").onclick = () => {
  const spots = loadSpots();
  showSheet(`<h3 id="sheet-title" tabindex="-1" autofocus>Save this spot</h3>
    <form id="spot-form" class="form"><label>Name <input id="spot-name" value="Spot ${spots.length + 1}" autocomplete="off"></label>
    <div class="actions"><button class="btn primary">Save</button></div></form>`);
  $("#spot-name").select();
  $("#spot-form").onsubmit = (e) => {
    e.preventDefault();
    const nodes = plAggregate().filter((n) => n.rep);
    spots.push({ name: $("#spot-name").value.trim() || `Spot ${spots.length + 1}`, ts: Date.now(),
      nodes: nodes.map((n) => ({ hop: n.hop, name: n.name, avg: +n.avg.toFixed(1), count: n.count,
        up: PL.traces[n.hop]?.up, down: PL.traces[n.hop]?.down })) });
    saveSpots(spots); $("#sheet").close(); renderSpots(); toast("Spot saved");
  };
};

function renderSpots() {
  const spots = loadSpots(), el = $("#pl-spots");
  if (!spots.length) { el.innerHTML = `<p class="hint">Save each spot you try, then compare them here.</p>`; return; }
  // columns: the repeaters heard most across all spots
  const freq = {};
  spots.forEach((sp) => sp.nodes.forEach((n) => { const f = freq[n.hop] ||= { name: n.name, k: 0, top: -99 }; f.k++; f.top = Math.max(f.top, n.avg); }));
  const cols = Object.entries(freq).sort((a, b) => b[1].k - a[1].k || b[1].top - a[1].top).slice(0, 4);
  const score = (sp) => Math.max(...sp.nodes.map((n) => n.avg), -99);
  const best = spots.reduce((b, sp, i) => (score(sp) > score(spots[b]) ? i : b), 0);
  const cell = (n) => !n ? `<td class="muted">–</td>`
    : `<td><span class="chip ${snrGrade(n.avg).cls}">${fmtDb(n.avg)}</span>${n.up != null ? `<small class="pl-ud">↑${fmtDb(n.up)} ↓${fmtDb(n.down)}</small>` : ""}</td>`;
  el.innerHTML = `<div class="scroll-x"><table class="tbl pl-spots"><thead><tr><th>Spot</th>${cols.map(([, c]) => `<th>${esc(c.name)}</th>`).join("")}<th></th></tr></thead>
    <tbody>${spots.map((sp, i) => `<tr class="${i === best && spots.length > 1 ? "pl-win" : ""}">
      <td><b>${esc(sp.name)}</b>${i === best && spots.length > 1 ? ` <span class="tag">best</span>` : ""}<br><small class="muted">${hhmm(sp.ts)}</small></td>
      ${cols.map(([hop]) => cell(sp.nodes.find((n) => n.hop === hop))).join("")}
      <td><button class="btn sm" data-del="${i}" aria-label="Delete ${esc(sp.name)}">✕</button></td></tr>`).join("")}</tbody></table></div>
    <div class="row-actions"><button class="btn sm" id="spots-clear">Clear all spots</button></div>`;
  el.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => { spots.splice(+b.dataset.del, 1); saveSpots(spots); renderSpots(); }));
  $("#spots-clear").onclick = async () => {
    if (await confirmSheet("Clear all saved spots?", "", "Clear", true)) { saveSpots([]); PL.bestEver = null; renderSpots(); }
  };
}

// ---------- map ----------
// Plots your contacts plus every node heard advertising with a location
// (including repeaters you've heard but never saved as contacts).
let map, layer, legend, trails, links, mapNodes = [];
async function showMap() {
  const my = nav;
  if (!map) {
    map = L.map("map", { zoomControl: true, attributionControl: true }).setView([S.state?.lat || 51.5, S.state?.lon || -0.1], S.state?.lat ? 11 : 6);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(map);
    layer = L.layerGroup().addTo(map);
    legend = L.control({ position: "bottomleft" });
    legend.onAdd = () => {
      const d = L.DomUtil.create("div", "map-legend");
      d.innerHTML = `<span><i class="lg-you"></i>You</span><span><i class="lg-rep"></i>Repeater</span><span><i class="lg-person"></i>Person</span><span><i class="lg-room"></i>Room</span>`;
      return d;
    };
    legend.addTo(map);
    links = L.layerGroup().addTo(map);   // under the trails
    trails = L.layerGroup().addTo(map);
    const live = L.control({ position: "topright" });
    live.onAdd = () => {
      const d = L.DomUtil.create("div", "map-live");
      d.innerHTML = `<label><input type="checkbox" id="trail-on" ${TR.on ? "checked" : ""}> Live packets</label>
        <label><input type="checkbox" id="links-on" ${TR.links ? "checked" : ""}> Links heard (24 h)</label><div id="trail-log" aria-live="polite"></div>`;
      L.DomEvent.disableClickPropagation(d);
      d.querySelector("#links-on").onchange = (e) => { TR.links = e.target.checked; try { localStorage.links = TR.links ? "1" : "0"; } catch {} drawLinks(); };
      d.querySelector("input").onchange = (e) => { TR.on = e.target.checked; try { localStorage.trails = TR.on ? "1" : "0"; } catch {} if (!TR.on) trails.clearLayers(); };
      return d;
    };
    live.addTo(map);
  }
  setTimeout(() => map.invalidateSize(), 50);
  let heard = [];
  try { heard = await api("/nearby?hours=168"); } catch { /* contacts only */ }
  if (my !== nav) return;
  layer.clearLayers();
  const colour = { 1: css("--link"), 2: css("--accent"), 3: css("--ok"), 4: css("--warn") };
  const nodes = new Map();
  for (const n of heard) if (n.lat || n.lon) nodes.set(n.pubkey, { key: n.pubkey, name: n.name || n.pubkey.slice(0, 8), type: n.type, lat: n.lat, lon: n.lon, snr: n.snr, last: n.last_ts, contact: n.is_contact });
  for (const c of S.contacts) if (c.lat || c.lon) nodes.set(c.public_key, { ...(nodes.get(c.public_key) || {}), key: c.public_key, name: c.name, type: c.type, lat: c.lat, lon: c.lon, contact: true });
  mapNodes = [...nodes.values()];
  const pts = [];
  for (const n of nodes.values()) {
    pts.push([n.lat, n.lon]);
    const link = n.contact ? `<br><a href="#/contact/${n.key}">Open contact</a>` : "";
    L.circleMarker([n.lat, n.lon], { radius: n.type === 2 ? 8 : 6, color: colour[n.type] || css("--muted"), weight: 2, fillOpacity: n.contact ? .55 : .25 })
      .bindPopup(`<b>${esc(n.name)}</b><br>${TYPES[n.type] || "node"}${n.snr != null ? ` · ${n.snr} dB` : ""}${n.last ? ` · heard ${ago(n.last)} ago` : ""}${link}`)
      .addTo(layer);
  }
  if (S.state?.lat) {
    pts.push([S.state.lat, S.state.lon]);
    L.circleMarker([S.state.lat, S.state.lon], { radius: 9, color: css("--fg"), weight: 3, fillOpacity: .9 }).bindPopup("You").addTo(layer);
  }
  if (S.focus) { map.setView([S.focus.lat, S.focus.lon], 13); S.focus = null; }
  else if (pts.length > 1) map.fitBounds(pts, { padding: [30, 30], maxZoom: 13 });
  else if (pts.length) map.setView(pts[0], 11);
  drawLinks();
  if (S.replay) { const p = S.replay; S.replay = null; setTimeout(() => trailPacket(p, true), 300); }
}

// ---------- live packet trails on the map ----------
// Replays each packet's route: sender → repeaters → you. Hops are 1- or 2-byte key
// prefixes, so we pick the located node with that prefix nearest the next point
// back along the path (the last hop is near you), preferring repeaters.
const TR = { on: (() => { try { return localStorage.trails !== "0"; } catch { return true; } })(), running: 0,
  links: (() => { try { return localStorage.links === "1"; } catch { return false; } })() };
const TRAIL_MS = 900;   // per leg

const kmBetween = (a, b) => {
  const r = Math.PI / 180, x = (b[1] - a[1]) * r * Math.cos(((a[0] + b[0]) / 2) * r), y = (b[0] - a[0]) * r;
  return Math.hypot(x, y) * 6371;
};

// Repeater-to-repeater links from every path heard in the last day (bridge
// /api/topology). Thicker = more packets; links to you are coloured by SNR.
async function drawLinks() {
  if (!links) return;
  links.clearLayers();
  if (!TR.links) return;
  let t; try { t = await api("/topology?hours=24"); } catch { return toast("Couldn't load links", "err"); }
  if (!TR.links) return;
  const at = new Map(t.nodes.map((n) => [n.key, n]));
  if (t.me) at.set("me", { ...t.me, name: "You" });
  const r = L.canvas({ padding: .3 });
  const grade = { "q-good": css("--ok"), "q-ok": css("--warn"), "q-bad": css("--bad") };
  for (const e of t.edges) {
    const a = at.get(e.a), b = at.get(e.b); if (!a || !b) continue;
    const mine = e.a === "me" || e.b === "me";
    L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
      renderer: r, color: mine ? grade[qClass(e.snr)] || css("--fg") : css("--link"),
      weight: Math.min(1 + Math.log2(e.count), 7), opacity: mine ? .9 : .45,
    }).bindPopup(`<b>${esc(a.name)} ↔ ${esc(b.name)}</b><br>${e.count} packets relayed · last ${hhmm(e.last)}${e.snr != null ? `<br>you hear it at ${e.snr} dB` : ""}`)
      .addTo(links);
  }
}

function resolveTrail(p) {
  const me = S.state?.lat ? [S.state.lat, S.state.lon] : null;
  const n = p.path_len || 0, size = n ? Math.max(1, Math.round(p.path.length / 2 / n)) : 1;
  const hops = p.ptype === "TRACE" ? [] : (p.path || "").match(new RegExp(`.{${2 * size}}`, "g")) || [];
  const pts = [];
  let next = me;
  for (const h of hops.reverse()) {
    const cands = mapNodes.filter((x) => x.key.startsWith(h));
    const reps = cands.filter((x) => x.type === 2 || x.type === 3);
    const pool = reps.length ? reps : cands;
    let best = null, bestKm = Infinity;
    for (const c of pool) { const km = next ? kmBetween(next, [c.lat, c.lon]) : 0; if (km < bestKm) { best = c; bestKm = km; } }
    if (best && bestKm < 150) { pts.unshift({ ll: [best.lat, best.lon], name: best.name }); next = [best.lat, best.lon]; }
    else pts.unshift(null);   // unknown or implausibly far: a gap in the trail
  }
  const origin = p.adv_key ? mapNodes.find((x) => x.key === p.adv_key)
    : p.sender ? mapNodes.find((x) => x.name === p.sender) : null;
  if (origin) pts.unshift({ ll: [origin.lat, origin.lon], name: origin.name, origin: true });
  if (me) pts.push({ ll: me, name: "You" });
  const known = pts.filter(Boolean);
  return { known, gaps: pts.length - known.length, hops: hops.length };
}

function trailPacket(p, replay = false) {
  if (!map || !trails || (!replay && TR.running >= 6)) return;
  const { known, gaps, hops } = resolveTrail(p);
  const log = $("#trail-log");
  if (log) {
    const via = p.heard_from && p.heard_from !== "direct" ? ` via ${p.heard_from}` : " direct";
    log.insertAdjacentHTML("afterbegin", `<div>${hhmm(p.ts)} ${esc(p.ptype)}${esc(via)} · ${hops} hop${hops === 1 ? "" : "s"}${gaps ? ` (${gaps} not on map)` : ""}</div>`);
    while (log.children.length > 4) log.lastChild.remove();
  }
  if (known.length < 2) { if (replay) toast("None of this packet's repeaters have a location on the map"); return; }
  if (replay) map.fitBounds(known.map((k) => k.ll), { padding: [40, 40], maxZoom: 13 });
  const colour = { GRP_TXT: css("--accent"), TEXT_MSG: css("--accent"), ADVERT: css("--link"), ACK: css("--ok") }[p.ptype] || css("--warn");
  const line = L.polyline([known[0].ll], { color: colour, weight: 3, opacity: .85, className: "trail" }).addTo(trails);
  const dot = L.circleMarker(known[0].ll, { radius: 5, color: colour, weight: 2, fillColor: colour, fillOpacity: 1 }).addTo(trails);
  dot.bindTooltip(known.map((k) => k.name).join(" → "));
  TR.running++;
  let leg = 0, t0 = performance.now();
  const step = (now) => {
    const a = known[leg].ll, b = known[leg + 1].ll, f = Math.min(1, (now - t0) / TRAIL_MS);
    const ll = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
    dot.setLatLng(ll);
    const lls = line.getLatLngs(); lls[leg + 1] = L.latLng(ll); line.setLatLngs(lls);
    if (f >= 1) {
      L.circleMarker(b, { radius: 9, color: colour, weight: 2, fillOpacity: 0, className: "trail-ping" }).addTo(trails);
      leg++; t0 = now;
    }
    if (leg < known.length - 1) return requestAnimationFrame(step);
    TR.running--;
    setTimeout(() => { trails.removeLayer(line); trails.removeLayer(dot); }, 4000);
    setTimeout(() => trails.eachLayer((l) => l.options.className === "trail-ping" && trails.removeLayer(l)), 1200);
  };
  requestAnimationFrame(step);
}

// ---------- settings ----------
async function fillFilters() {
  let f; try { f = await api("/filters"); } catch { return; }
  const form = $("#filters");
  form.sender_regex.value = (f.sender_regex || []).join("\n");
  form.text_regex.value = (f.text_regex || []).join("\n");
  form.flood.checked = !!f.flood;
}
$("#filters").onsubmit = async (e) => {
  e.preventDefault();
  const form = e.target, lines = (t) => t.value.split("\n").map((s) => s.trim()).filter(Boolean);
  try {
    const r = await api("/filters", { method: "POST", body: JSON.stringify({
      sender_regex: lines(form.sender_regex), text_regex: lines(form.text_regex), flood: form.flood.checked }) });
    toast(`Filters saved — ${r.hidden} message${r.hidden === 1 ? "" : "s"} from the last 7 days hidden`);
  } catch (err) { fail(err, "Couldn't save filters"); }
};

function fillSettings() {
  const s = S.state; if (!s) return;
  const f = $("#settings");
  f.name.value = s.name || ""; f.tx_power.value = s.tx_power ?? "";
  f.tx_power.max = s.max_tx_power || 30;
  f.lat.value = s.lat || ""; f.lon.value = s.lon || "";
  f.manual_add_contacts.checked = !!s.manual_add_contacts;
  // the Bluetooth PIN means nothing when the radio is on USB
  const pinLabel = f.ble_pin.closest("label"), usb = s.link === "usb";
  pinLabel.hidden = usb;
  if (pinLabel.nextElementSibling?.classList.contains("hint")) pinLabel.nextElementSibling.hidden = usb;
  f.auto_manage.checked = s.auto_manage !== false;
  f.ble_pin.value = "";
  f.ble_pin.placeholder = s.device?.ble_pin ? `currently fixed (${s.device.ble_pin})` : "currently random each boot";
  loadRadioSettings();
}

// Everything else the radio exposes (LoRa, telemetry, tuning, board variables like GPS).
const RS_FIELDS = { radio_freq: "radio_freq", radio_bw: "radio_bw", radio_sf: "radio_sf", radio_cr: "radio_cr",
  adv_loc_policy: "adv_loc_policy", telemetry_base: "telemetry_mode_base", telemetry_loc: "telemetry_mode_loc",
  telemetry_env: "telemetry_mode_env", path_hash_mode: "path_hash_mode", tz_offset: "tz_offset",
  airtime_factor: "airtime_factor", rx_delay: "rx_delay" };
async function loadRadioSettings() {
  let r; try { r = S.rs = await api("/radio/settings"); } catch { return; }
  const f = $("#settings");
  for (const [field, key] of Object.entries(RS_FIELDS)) if (f[field] && r[key] != null) f[field].value = r[key];
  f.multi_acks.checked = !!r.multi_acks;
  f.tz_offset.closest("label").hidden = r.tz_offset == null;   // firmware before v1.18 has no CLI
  $("#lora-unlock").checked = false; $("#lora").disabled = true;
  $("#lora-preset").value = [r.radio_freq, r.radio_bw, r.radio_sf, r.radio_cr].join(",");
  const cv = r.custom || {};
  $("#gps-box").innerHTML = ("gps" in cv
    ? `<label class="check"><input id="cv-gps" type="checkbox" ${cv.gps === "1" ? "checked" : ""}> GPS on</label>
       <small class="hint">With GPS on, the radio keeps its own location up to date (the fields above are then overwritten).</small>`
    : `<small class="hint">No GPS module detected. The radio only offers GPS when one is plugged into its GPS connector at power-on.</small>`)
    + Object.keys(cv).filter((k) => k !== "gps").map((k) => `<label>${esc(k)} <input data-cv="${esc(k)}" value="${esc(cv[k])}"></label>`).join("");
}
$("#lora-unlock").onchange = (e) => ($("#lora").disabled = !e.target.checked);
$("#lora-preset").onchange = (e) => {
  if (!e.target.value) return;
  const [fr, bw, sf, cr] = e.target.value.split(","), f = $("#settings");
  f.radio_freq.value = fr; f.radio_bw.value = bw; f.radio_sf.value = sf; f.radio_cr.value = cr;
};
$("#settings").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target, body = {};
  if (f.name.value && f.name.value !== S.state.name) body.name = f.name.value;
  if (f.tx_power.value && +f.tx_power.value !== S.state.tx_power) body.tx_power = +f.tx_power.value;
  if (f.lat.value && f.lon.value && (+f.lat.value !== S.state.lat || +f.lon.value !== S.state.lon)) { body.lat = +f.lat.value; body.lon = +f.lon.value; }
  if (f.manual_add_contacts.checked !== !!S.state.manual_add_contacts) body.manual_add_contacts = f.manual_add_contacts.checked;
  if (f.auto_manage.checked !== (S.state.auto_manage !== false)) body.auto_manage = f.auto_manage.checked;
  if (f.ble_pin.value) body.ble_pin = +f.ble_pin.value;
  const r = S.rs;
  if (r) {
    const num = (k) => (f[k].value === "" ? null : +f[k].value);
    if (!$("#lora").disabled) {
      const lora = ["radio_freq", "radio_bw", "radio_sf", "radio_cr"].map(num);
      if (lora.some((v, i) => v !== r[["radio_freq", "radio_bw", "radio_sf", "radio_cr"][i]])) {
        if (lora.includes(null)) return toast("Fill in all four LoRa values", "err");
        [body.radio_freq, body.radio_bw, body.radio_sf, body.radio_cr] = lora;
      }
    }
    for (const k of ["adv_loc_policy", "telemetry_base", "telemetry_loc", "telemetry_env", "path_hash_mode", "tz_offset", "airtime_factor", "rx_delay"])
      if (num(k) != null && num(k) !== r[RS_FIELDS[k]]) body[k] = num(k);
    if (f.multi_acks.checked !== !!r.multi_acks) body.multi_acks = f.multi_acks.checked ? 1 : 0;
    const custom = {};
    const gps = $("#cv-gps");
    if (gps && (gps.checked ? "1" : "0") !== r.custom.gps) custom.gps = gps.checked ? "1" : "0";
    f.querySelectorAll("[data-cv]").forEach((i) => { if (i.value !== r.custom[i.dataset.cv]) custom[i.dataset.cv] = i.value; });
    if (Object.keys(custom).length) body.custom = custom;
  }
  if (!Object.keys(body).length) return toast("Nothing changed");
  const btn = f.querySelector("button.primary"); btn.disabled = true;
  try { S.state = { ...S.state, ...(await api("/settings", { method: "POST", body: JSON.stringify(body) })) }; renderRadio(); fillSettings(); toast(`Saved to radio: ${Object.keys(body).length} setting${Object.keys(body).length === 1 ? "" : "s"}`); }
  catch (err) { fail(err, "Couldn't save"); }
  finally { btn.disabled = false; }
};

// ---------- layout: keep the composer above the iOS keyboard ----------
const setKb = () => {
  const vv = window.visualViewport; if (!vv) return;
  // keyboard height = layout viewport minus visible part; tiny values are rounding noise
  let kb = Math.round(window.innerHeight - vv.height - vv.offsetTop);
  if (kb < 40) kb = 0;
  document.documentElement.style.setProperty("--kb", kb + "px");
  if (kb && ["dm", "chan"].includes(S.route[0]) && document.activeElement === text) scroller().scrollTop = 1e9;
};
window.visualViewport?.addEventListener("resize", setKb);
// iOS/iPadOS leave the page scrolled up after the keyboard closes, pushing the
// fixed composer off-screen. Put everything back once it's gone.
text.addEventListener("blur", () => setTimeout(() => {
  window.scrollTo(0, 0); document.documentElement.scrollTop = 0; document.body.scrollTop = 0;
  setKb();
}, 150));
text.addEventListener("focus", () => setTimeout(setKb, 300));
window.visualViewport?.addEventListener("scroll", setKb);
new ResizeObserver(([e]) => document.documentElement.style.setProperty("--composer-h", e.target.offsetHeight + "px"))
  .observe($("#composer"));

// ---------- theme ----------
// Mode (System/Light/Dark) and colour theme are separate: every theme has a
// light and a dark set in style.css. Swatches: light bg, accent, link, dark bg, dark accent.
const PALETTES = [
  { id: "", name: "Kanagawa", sw: ["#fbfaf7", "#c84053", "#4d699b", "#16161d", "#e46876"] },
  { id: "nord", name: "Nord", sw: ["#eceff4", "#5e81ac", "#4a6c94", "#2e3440", "#88c0d0"] },
  { id: "gruvbox", name: "Gruvbox", sw: ["#fbf1c7", "#af3a03", "#076678", "#282828", "#fe8019"] },
  { id: "solarized", name: "Solarized", sw: ["#fdf6e3", "#268bd2", "#6c71c4", "#002b36", "#2aa198"] },
  { id: "contrast", name: "High contrast", sw: ["#ffffff", "#0040dd", "#000000", "#000000", "#ffd400"] },
];
const darkMq = matchMedia("(prefers-color-scheme: dark)");
const stored = (k, d) => { try { return localStorage.getItem(k) || d; } catch { return d; } };
let themePref = stored("theme", "system"), palettePref = stored("palette", "");
$("#palettes").innerHTML = PALETTES.map((p) => `<button type="button" data-palette="${p.id}" aria-pressed="false">
  <span class="sw">${p.sw.map((c) => `<i style="background:${c}"></i>`).join("")}</span>${p.name}</button>`).join("");
function applyTheme(t = themePref, pal = palettePref) {
  themePref = t; palettePref = pal;
  const root = document.documentElement;
  if (t === "light" || t === "dark") root.dataset.theme = t; else delete root.dataset.theme;
  if (pal) root.dataset.palette = pal; else delete root.dataset.palette;
  try {
    t === "system" ? localStorage.removeItem("theme") : localStorage.setItem("theme", t);
    pal ? localStorage.setItem("palette", pal) : localStorage.removeItem("palette");
  } catch { /* private mode */ }
  document.querySelectorAll("#theme button").forEach((b) => b.classList.toggle("on", b.dataset.theme === t));
  document.querySelectorAll("#palettes button").forEach((b) => {
    const on = b.dataset.palette === pal; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on);
  });
  const bg = css("--bg");
  document.querySelectorAll('meta[name="theme-color"]').forEach((m) => m.setAttribute("content", bg));
  if (["signal", "radio"].includes(S.route[0])) loadStats();   // charts take their colours at draw time
  if (S.route[0] === "map" && map) showMap();
}
$("#theme").onclick = (e) => { const t = e.target.dataset.theme; if (t) applyTheme(t); };
$("#palettes").onclick = (e) => { const b = e.target.closest("[data-palette]"); if (b) applyTheme(themePref, b.dataset.palette); };
darkMq.addEventListener?.("change", () => { if (themePref === "system") applyTheme(); });
applyTheme();

// Charts are drawn at a fixed width: follow rotation / window resizes.
window.addEventListener("resize", debounce(() => {
  for (const [id, u] of Object.entries(charts)) { const el = $(id); if (el?.clientWidth) u.setSize({ width: el.clientWidth, height: 180 }); }
}, 200));

// ---------- boot ----------
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => { /* offline cache is optional */ });
(async () => {
  try { await refreshState(); S.contacts = await api("/contacts"); } catch { /* bridge down: still render */ }
  connectStream();
  go();
  syncPush();
})();
