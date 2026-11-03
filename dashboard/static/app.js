"use strict";
/* Dashboard front-end.
   Security note: server data (channel names, reasons...) is ONLY ever put on the page with
   textContent / createTextNode / setAttribute, never innerHTML, so a hostile channel name
   cannot inject markup or script. */
(() => {
  const S = {
    csrf: null, groq: false, gid: null, info: null, live: null, idx: null,
    draft: null, prevDraft: null, drag: null, key: 0,
  };
  const $ = (sel) => document.querySelector(sel);
  const TYPE_ICON = { text: "#", voice: "🔊", stage: "🎙️", forum: "💬", announcement: "📢", media: "🖼️", other: "•" };
  const nk = () => "k" + (++S.key);

  function el(tag, attrs = {}, ...kids) {
    const node = document.createElement(tag);
    for (const [key, val] of Object.entries(attrs)) {
      if (val === false || val == null) continue;
      if (key === "class") node.className = val;
      else if (key === "text") node.textContent = val;
      else node.setAttribute(key, val === true ? "" : String(val));
    }
    for (const kid of kids.flat()) if (kid != null) node.append(kid);
    return node;
  }
  const on = (node, ev, fn) => { node.addEventListener(ev, fn); return node; };

  let toastTimer = null;
  function toast(message, kind = "") {
    const t = $("#toast");
    t.textContent = message;
    t.className = "toast " + kind;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), kind === "err" ? 8000 : 4500);
  }

  async function api(path, { method = "GET", body, form } = {}) {
    const headers = {};
    if (method !== "GET") headers["X-CSRF-Token"] = S.csrf;
    let payload;
    if (form) payload = form;
    else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    const res = await fetch(path, { method, headers, body: payload, credentials: "same-origin" });
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error */ }
    if (res.status === 401) { location.href = "/login"; throw new Error("Signed out."); }
    if (!res.ok) { const err = new Error(data.error || `Request failed (${res.status}).`); err.status = res.status; throw err; }
    return data;
  }

  // ------------------------------------------------------------ draft helpers
  function buildIndex(layout) {
    const names = {}, parent = {};
    for (const c of layout.categories) {
      names[c.id] = c.name;
      for (const ch of c.channels) { names[ch.id] = ch.name; parent[ch.id] = c.id; }
    }
    for (const ch of layout.uncategorized) { names[ch.id] = ch.name; parent[ch.id] = null; }
    return { names, parent };
  }

  function decorate(draft) {
    const tag = (x) => { x._k = nk(); x._orig = x.id ? (S.idx.names[x.id] ?? null) : null; return x; };
    draft.uncategorized.forEach(tag);
    draft.categories.forEach((c) => { tag(c); c.channels.forEach(tag); });
    draft.deleted = draft.deleted || [];
    return draft;
  }
  const plain = (draft) => JSON.parse(JSON.stringify(draft, (k, v) => (k.startsWith("_") ? undefined : v)));
  const allLists = () => [S.draft.uncategorized, ...S.draft.categories.map((c) => c.channels)];

  function takeChannel(k) {
    for (const list of allLists()) {
      const i = list.findIndex((x) => x._k === k);
      if (i >= 0) return list.splice(i, 1)[0];
    }
    return null;
  }
  function moveChannel(k, list, beforeK) {
    const item = takeChannel(k);
    if (!item) return;
    let i = beforeK ? list.findIndex((x) => x._k === beforeK) : -1;
    if (i < 0) i = list.length;
    list.splice(i, 0, item);
    render();
  }
  function moveCategory(k, beforeK) {
    const cats = S.draft.categories;
    const i = cats.findIndex((c) => c._k === k);
    if (i < 0) return;
    const [cat] = cats.splice(i, 1);
    let j = beforeK ? cats.findIndex((c) => c._k === beforeK) : -1;
    if (j < 0) j = cats.length;
    cats.splice(j, 0, cat);
    render();
  }
  function nudge(list, k, dir) {
    const i = list.findIndex((x) => x._k === k), j = i + dir;
    if (i < 0 || j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    render();
  }
  function addChannel(list, type) {
    const ch = { _k: nk(), id: null, name: type === "voice" ? "New Voice" : "new-channel", type, topic: null, why: null, _orig: null };
    list.push(ch);
    render();
    const input = document.querySelector(`[data-k="${ch._k}"] input[type=text]`);
    if (input) { input.focus(); input.select(); }
  }
  function addCategory() {
    const cat = { _k: nk(), id: null, name: "New Category", why: null, channels: [], _orig: null };
    S.draft.categories.push(cat);
    render();
    const input = document.querySelector(`[data-k="${cat._k}"] input[type=text]`);
    if (input) { input.focus(); input.select(); }
  }
  function deleteChannel(ch, list) {
    list.splice(list.indexOf(ch), 1);
    if (ch.id) S.draft.deleted.push({ id: ch.id, name: ch._orig ?? ch.name, type: ch.type });
    render();
  }
  function deleteCategory(cat) {
    S.draft.uncategorized.push(...cat.channels.splice(0));
    S.draft.categories = S.draft.categories.filter((c) => c !== cat);
    if (cat.id) S.draft.deleted.push({ id: cat.id, name: cat._orig ?? cat.name, type: "category" });
    render();
    toast("Category removed. Its channels were moved to \"No category\".");
  }
  function restore(entry) {
    S.draft.deleted = S.draft.deleted.filter((d) => d !== entry);
    if (entry.type === "category") S.draft.categories.push({ _k: nk(), id: entry.id, name: entry.name, why: null, channels: [], _orig: entry.name });
    else S.draft.uncategorized.push({ _k: nk(), id: entry.id, name: entry.name, type: entry.type, topic: null, why: null, _orig: entry.name });
    render();
  }

  // --------------------------------------------------------------- rendering
  function clearDropMarks() { document.querySelectorAll(".drop-before").forEach((n) => n.classList.remove("drop-before")); }

  // "was: old name" label that updates live as you type, WITHOUT re-rendering (a re-render on blur
  // would replace a button between mouse-down and mouse-up and swallow the click).
  function wasNode(item) {
    const node = el("div", { class: "was hidden" });
    const update = () => {
      const changed = item.id && item._orig != null && item._orig !== item.name;
      node.textContent = changed ? `was: ${item._orig}` : "";
      node.classList.toggle("hidden", !changed);
    };
    update();
    return { node, update };
  }

  function channelRow(ch, list, parentId) {
    const row = el("div", { class: "item" + (ch.id ? "" : " is-new"), "data-k": ch._k });
    const handle = el("span", { class: "handle", title: "Drag to move", draggable: "true", text: "⋮⋮" });
    on(handle, "dragstart", (e) => {
      S.drag = { type: "ch", k: ch._k };
      e.dataTransfer.setData("text/plain", ch._k);
      e.dataTransfer.effectAllowed = "move";
      row.classList.add("dragging");
    });
    on(handle, "dragend", () => { S.drag = null; row.classList.remove("dragging"); clearDropMarks(); });
    on(row, "dragover", (e) => {
      if (S.drag?.type === "ch" && S.drag.k !== ch._k) { e.preventDefault(); e.stopPropagation(); clearDropMarks(); row.classList.add("drop-before"); }
    });
    on(row, "drop", (e) => {
      if (S.drag?.type !== "ch") return;
      e.preventDefault(); e.stopPropagation();
      const k = S.drag.k; S.drag = null;
      if (k !== ch._k) moveChannel(k, list, ch._k);
    });

    const was = wasNode(ch);
    const input = el("input", { type: "text", value: ch.name, maxlength: "100", "aria-label": "Channel name" });
    on(input, "input", () => { ch.name = input.value; was.update(); });

    const move = el("select", { "aria-label": "Move to category", title: "Move to category" });
    move.append(el("option", { value: "", text: "No category" }));
    S.draft.categories.forEach((c) => move.append(el("option", { value: c._k, text: c.name || "(unnamed)" })));
    const here = S.draft.categories.find((c) => c.channels === list);
    move.value = here ? here._k : "";
    on(move, "change", () => {
      const target = move.value ? S.draft.categories.find((c) => c._k === move.value).channels : S.draft.uncategorized;
      moveChannel(ch._k, target, null);
    });

    row.append(
      handle,
      el("span", { class: "type", title: ch.type, text: TYPE_ICON[ch.type] || "•" }),
      input,
      !ch.id ? el("span", { class: "badge new", text: "NEW" }) : null,
      ch.id && S.idx.parent[ch.id] !== parentId ? el("span", { class: "badge moved", text: "MOVED" }) : null,
      on(el("button", { class: "mini ghost", type: "button", title: "Move up", "aria-label": "Move up", text: "↑" }), "click", () => nudge(list, ch._k, -1)),
      on(el("button", { class: "mini ghost", type: "button", title: "Move down", "aria-label": "Move down", text: "↓" }), "click", () => nudge(list, ch._k, 1)),
      move,
      on(el("button", { class: "mini ghost danger", type: "button", title: "Delete", "aria-label": "Delete channel", text: "✕" }), "click", () => deleteChannel(ch, list)),
    );
    row.append(was.node);
    if (ch.why) row.append(el("div", { class: "why", text: `💡 ${ch.why}` }));
    return row;
  }

  function listZone(list, parentId) {
    const zone = el("div", { class: "list" });
    if (!list.length) zone.append(el("div", { class: "empty-hint", text: "Drop channels here" }));
    list.forEach((ch) => zone.append(channelRow(ch, list, parentId)));
    on(zone, "dragover", (e) => { if (S.drag?.type === "ch") e.preventDefault(); });
    on(zone, "drop", (e) => {
      if (S.drag?.type !== "ch") return;
      e.preventDefault();
      const k = S.drag.k; S.drag = null;
      moveChannel(k, list, null);
    });
    return zone;
  }

  function categoryBlock(cat) {
    const block = el("section", { class: "cat", "data-k": cat._k });
    const handle = el("span", { class: "handle", title: "Drag to reorder category", draggable: "true", text: "⋮⋮" });
    on(handle, "dragstart", (e) => {
      S.drag = { type: "cat", k: cat._k };
      e.dataTransfer.setData("text/plain", cat._k);
      e.dataTransfer.effectAllowed = "move";
      block.classList.add("dragging");
    });
    on(handle, "dragend", () => { S.drag = null; block.classList.remove("dragging"); clearDropMarks(); });
    on(block, "dragover", (e) => { if (S.drag?.type === "cat" && S.drag.k !== cat._k) { e.preventDefault(); clearDropMarks(); block.classList.add("drop-before"); } });
    on(block, "drop", (e) => {
      if (S.drag?.type !== "cat") return;
      e.preventDefault();
      const k = S.drag.k; S.drag = null;
      if (k !== cat._k) moveCategory(k, cat._k);
    });

    const was = wasNode(cat);
    const input = el("input", { type: "text", value: cat.name, maxlength: "100", "aria-label": "Category name" });
    on(input, "input", () => {
      cat.name = input.value;
      was.update();
      // keep every channel's "Move to category" menu in sync without re-rendering
      document.querySelectorAll("#editor option").forEach((o) => { if (o.value === cat._k) o.textContent = cat.name || "(unnamed)"; });
    });
    const cats = S.draft.categories;
    const head = el("div", { class: "cat-head" },
      handle, input,
      !cat.id ? el("span", { class: "badge new", text: "NEW" }) : null,
      on(el("button", { class: "mini ghost", type: "button", "aria-label": "Move category up", text: "↑" }), "click", () => nudge(cats, cat._k, -1)),
      on(el("button", { class: "mini ghost", type: "button", "aria-label": "Move category down", text: "↓" }), "click", () => nudge(cats, cat._k, 1)),
      on(el("button", { class: "mini ghost", type: "button", text: "+ text" }), "click", () => addChannel(cat.channels, "text")),
      on(el("button", { class: "mini ghost", type: "button", text: "+ voice" }), "click", () => addChannel(cat.channels, "voice")),
      on(el("button", { class: "mini ghost danger", type: "button", "aria-label": "Delete category", text: "✕" }), "click", () => {
        if (!cat.id || confirm(`Mark the category "${cat._orig ?? cat.name}" for deletion? Its channels move to "No category".`)) deleteCategory(cat);
      }),
    );
    block.append(head);
    block.append(was.node);
    if (cat.why) block.append(el("div", { class: "why", text: `💡 ${cat.why}` }));
    block.append(listZone(cat.channels, cat.id));
    return block;
  }

  function render() {
    const root = $("#editor");
    root.replaceChildren();
    const top = el("section", { class: "cat top-block" },
      el("div", { class: "cat-head" },
        el("strong", { text: "No category" }),
        el("span", { class: "grow" }),
        on(el("button", { class: "mini ghost", type: "button", text: "+ text" }), "click", () => addChannel(S.draft.uncategorized, "text")),
        on(el("button", { class: "mini ghost", type: "button", text: "+ voice" }), "click", () => addChannel(S.draft.uncategorized, "voice")),
      ),
      listZone(S.draft.uncategorized, null),
    );
    root.append(top);
    S.draft.categories.forEach((c) => root.append(categoryBlock(c)));

    const trash = $("#trash");
    trash.replaceChildren();
    S.draft.deleted.forEach((d) => trash.append(el("div", { class: "trash-item" },
      el("span", { text: `${d.type}: ${d.name}` }),
      on(el("button", { class: "mini ghost", type: "button", text: "Restore" }), "click", () => restore(d)),
    )));
    $("#trash-wrap").classList.toggle("hidden", S.draft.deleted.length === 0);
  }

  // ------------------------------------------------------------------- server
  function renderServerCard() {
    const info = S.info;
    $("#guild-name").textContent = info.name;
    const img = $("#guild-icon");
    if (info.icon_url) { img.src = info.icon_url; img.classList.remove("hidden"); $("#guild-fallback").classList.add("hidden"); }
    else { img.classList.add("hidden"); $("#guild-fallback").classList.remove("hidden"); $("#guild-fallback").textContent = info.name.slice(0, 1).toUpperCase(); }
    const missing = [];
    if (!info.can_manage_channels) missing.push("Manage Channels (needed to organise channels)");
    if (!info.can_manage_guild) missing.push("Manage Server (needed to change the icon)");
    const warn = $("#perm-warning");
    warn.textContent = missing.length ? "The bot is missing: " + missing.join(" and ") + "." : "";
    warn.classList.toggle("hidden", !missing.length);
    $("#icon-remove").disabled = !info.icon_url;
  }

  async function loadGuild(gid) {
    S.gid = gid;
    $("#main").classList.add("hidden");
    try {
      const out = await api(`/api/guilds/${gid}/layout`);
      S.info = out.guild; S.live = out.layout; S.idx = buildIndex(out.layout);
      S.draft = decorate(out.draft); S.prevDraft = null;
      $("#ai-undo").classList.add("hidden"); $("#ai-notes").classList.add("hidden");
      renderServerCard(); render();
      $("#main").classList.remove("hidden");
    } catch (e) { toast(e.message, "err"); }
  }

  async function uploadIcon(remove) {
    const form = new FormData();
    if (remove) form.append("remove", "1");
    else form.append("icon", $("#icon-file").files[0]);
    const up = $("#icon-upload"); up.disabled = true;
    try {
      const out = await api(`/api/guilds/${S.gid}/icon`, { method: "POST", form });
      S.info = out.guild; renderServerCard();
      $("#icon-file").value = "";
      toast(remove ? "Server icon removed." : "Server icon updated.", "ok");
    } catch (e) { toast(e.message, "err"); }
    up.disabled = !$("#icon-file").files.length;
  }

  // ----------------------------------------------------------------------- AI
  async function runAI() {
    const btn = $("#ai-run");
    btn.disabled = true; btn.textContent = "Asking Groq…";
    try {
      const out = await api(`/api/guilds/${S.gid}/ai`, {
        method: "POST",
        body: {
          draft: plain(S.draft),
          options: {
            fix_spelling: $("#opt-spelling").checked, add_emojis: $("#opt-emojis").checked,
            reorder: $("#opt-reorder").checked, create_new: $("#opt-new").checked,
            emoji_separator: $("#opt-sep").value, instructions: $("#opt-instructions").value,
          },
        },
      });
      S.prevDraft = plain(S.draft);
      S.draft = decorate(out.draft);
      render();
      const notes = $("#ai-notes");
      notes.textContent = (out.notes || "Suggestions loaded.") + (out.warnings?.length ? "  (" + out.warnings.join(" ") + ")" : "");
      notes.classList.remove("hidden");
      $("#ai-undo").classList.remove("hidden");
      toast("Groq's suggestions are loaded below. Edit anything, then press Preview & apply.", "ok");
    } catch (e) { toast(e.message, "err"); }
    btn.disabled = false; btn.textContent = "Generate suggestions";
  }

  // ------------------------------------------------------------------- modal
  function showModal(title, body, actions) {
    $("#modal-title").textContent = title;
    $("#modal-body").replaceChildren(...body);
    $("#modal-actions").replaceChildren(...actions);
    $("#overlay").classList.remove("hidden");
  }
  const closeModal = () => $("#overlay").classList.add("hidden");
  const closeBtn = (label = "Back to editing") => on(el("button", { class: "ghost", type: "button", text: label }), "click", closeModal);

  async function openReview() {
    showModal("Reviewing…", [el("p", { class: "muted", text: "Working out exactly what would change…" })], []);
    try {
      const out = await api(`/api/guilds/${S.gid}/preview`, { method: "POST", body: { draft: plain(S.draft) } });
      S.draft = decorate(out.draft);
      render();
      renderReview(out);
    } catch (e) { closeModal(); toast(e.message, "err"); }
  }

  const KIND_LABEL = { delete: "Delete", create_category: "Create", create_channel: "Create", rename: "Rename", move: "Move", reorder: "Reorder" };

  function renderReview(out) {
    const body = [];
    if (out.errors.length) {
      body.push(el("p", { class: "error", text: "Fix these before applying:" }), el("ul", {}, out.errors.map((t) => el("li", { text: t }))));
      return showModal("Can't apply yet", body, [closeBtn()]);
    }
    if (!out.ops.length) return showModal("No changes", [el("p", { class: "muted", text: "The draft matches your server, so there's nothing to apply." })], [closeBtn("Close")]);
    if (out.warnings.length) body.push(el("p", { class: "warn", text: out.warnings.join(" ") }));
    body.push(el("p", { class: "muted", text: `${out.ops.length} change(s). Review them carefully. This is exactly what will be sent to Discord.` }));
    out.ops.forEach((op) => {
      const row = el("div", { class: "op " + op.kind }, el("span", { class: "kind", text: KIND_LABEL[op.kind] }));
      const text = el("div", {}, el("div", { text: op.text }));
      if (op.kind === "reorder") {
        text.append(el("div", { class: "reorder-detail", text: "Before: " + op.before.join(", ") }), el("div", { class: "reorder-detail", text: "After: " + op.after.join(", ") }));
      }
      row.append(text);
      body.push(row);
    });
    const hasDelete = out.ops.some((o) => o.kind === "delete");
    const hasMove = out.ops.some((o) => o.kind === "move");
    const confirmDel = el("input", { type: "checkbox" });
    const sync = el("input", { type: "checkbox" });
    const apply = on(el("button", { class: "primary", type: "button", text: `Apply ${out.ops.length} change(s)` }), "click", () => doApply(out, confirmDel.checked, sync.checked));
    apply.disabled = hasDelete;
    if (hasDelete) {
      on(confirmDel, "change", () => { apply.disabled = !confirmDel.checked; });
      body.push(el("label", { class: "warn" }, confirmDel, " I understand deleted channels and their messages can't be recovered."));
    }
    if (hasMove) body.push(el("label", {}, sync, " Make moved channels inherit their new category's permissions"));
    showModal("Review changes", body, [closeBtn(), apply]);
  }

  async function doApply(out, confirmDeletes, syncPermissions) {
    showModal("Applying…", [el("p", { class: "muted", text: "Sending changes to Discord. Large servers can take a minute." })], []);
    try {
      const res = await api(`/api/guilds/${S.gid}/apply`, {
        method: "POST",
        body: { draft: plain(S.draft), fingerprint: out.fingerprint, confirm_deletes: confirmDeletes, sync_permissions: syncPermissions },
      });
      pollJob(res.job);
    } catch (e) {
      if (e.status === 409) { toast(e.message, "err"); openReview(); }
      else { closeModal(); toast(e.message, "err"); }
    }
  }

  function pollJob(jobId) {
    const tick = async () => {
      try {
        const job = await api(`/api/jobs/${jobId}`);
        const list = el("div", {}, job.results.map((r) => el("div", { class: "result " + (r.ok ? "ok" : "bad"), text: r.text + (r.error ? ` - ${r.error}` : "") })));
        if (job.status === "running") {
          showModal("Applying…", [el("p", { class: "muted", text: `${job.results.length} step(s) done so far…` }), list], []);
          return setTimeout(tick, 1000);
        }
        showModal(job.status === "done" ? "Finished" : "Something went wrong", [el("p", { text: job.summary }), list],
          [on(el("button", { class: "primary", type: "button", text: "Close" }), "click", () => { closeModal(); loadGuild(S.gid); })]);
      } catch (e) { closeModal(); toast(e.message, "err"); }
    };
    tick();
  }

  // -------------------------------------------------------------------- start
  async function init() {
    try {
      const session = await api("/api/session");
      S.csrf = session.csrf; S.groq = session.groq;
      $("#ai-off").classList.toggle("hidden", S.groq);
      $("#ai-run").disabled = !S.groq;
      const { guilds } = await api("/api/guilds");
      const select = $("#guild");
      select.replaceChildren(...guilds.map((g) => el("option", { value: g.id, text: g.name })));
      if (!guilds.length) { $("#empty").textContent = "The bot isn't in any server that the dashboard can manage."; return; }
      $("#empty").classList.add("hidden");
      on(select, "change", () => loadGuild(select.value));
      loadGuild(select.value);
    } catch (e) { $("#empty").textContent = e.message; }
  }

  on($("#logout"), "click", async () => { try { await api("/api/logout", { method: "POST" }); } catch (_) { /* ignore */ } location.href = "/login"; });
  on($("#icon-file"), "change", () => { $("#icon-upload").disabled = !$("#icon-file").files.length; });
  on($("#icon-upload"), "click", () => uploadIcon(false));
  on($("#icon-remove"), "click", () => { if (confirm("Remove this server's icon?")) uploadIcon(true); });
  on($("#ai-run"), "click", runAI);
  on($("#ai-undo"), "click", () => { if (S.prevDraft) { S.draft = decorate(S.prevDraft); S.prevDraft = null; render(); $("#ai-undo").classList.add("hidden"); $("#ai-notes").classList.add("hidden"); } });
  on($("#add-category"), "click", addCategory);
  on($("#reset"), "click", () => { if (confirm("Discard all edits and reload the live layout?")) loadGuild(S.gid); });
  on($("#review"), "click", openReview);
  init();
})();
