/* Admin dashboard. Every action calls an /api/admin/* route that re-checks ADMIN_ID on
   the server; this file only decides what to show. Server text is never inserted as HTML. */
(function () {
  "use strict";
  var api = window.IGCommon.api;
  var toast = window.IGCommon.toast;
  var isAuthLost = window.IGCommon.isAuthLost;
  var main = document.getElementById("admin-main");
  var PAGE = 25;

  function h(tag, props, kids) {
    var n = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      var v = props[k];
      if (v === undefined || v === null || v === false) return;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = String(v);
      else if (k === "value") n.value = String(v);
      else if (k === "checked") n.checked = !!v;
      else if (k.indexOf("on") === 0 && typeof v === "function") n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, String(v));
    });
    (kids || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      n.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    });
    return n;
  }
  function fail(err) {
    if (isAuthLost(err)) { window.location.assign("/login"); return; }
    if (err && err.status === 403) { toast("Only the administrator can do this."); return; }
    toast((err && err.message) || "Something went wrong.", 4000);
  }
  function box(kind, text) { return h("div", { class: "msg-box " + kind, role: "status", text: text }); }
  function fmt(v) { return v === null || v === undefined || v === "" ? "—" : String(v); }
  function when(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  }
  function table(headers, rows) {
    var thead = h("thead", null, [h("tr", null, headers.map(function (t) { return h("th", { text: t }); }))]);
    var tbody = h("tbody", null, rows.map(function (cells) {
      return h("tr", null, cells.map(function (c) {
        return h("td", { class: c && c.wrap ? "wrap" : null }, [c && c.node ? c.node : (c && c.wrap ? c.text : c)]);
      }));
    }));
    if (!rows.length) tbody.appendChild(h("tr", null, [h("td", { colspan: headers.length, class: "muted", text: "Nothing to show." })]));
    return h("div", { class: "table-wrap" }, [h("table", null, [thead, tbody])]);
  }
  function pager(page, total, onPage) {
    var pages = Math.max(1, Math.ceil(total / PAGE));
    return h("div", { class: "pager" }, [
      h("button", { type: "button", disabled: page <= 1, onclick: function () { onPage(page - 1); } }, ["← Prev"]),
      h("span", { text: "Page " + page + " of " + pages + " · " + total + " total" }),
      h("button", { type: "button", disabled: page >= pages, onclick: function () { onPage(page + 1); } }, ["Next →"]),
    ]);
  }
  function button(label, onclick, cls) {
    return h("button", { type: "button", class: cls || null, text: label, onclick: onclick });
  }
  function setMain(title, nodes) {
    main.textContent = "";
    main.appendChild(h("h2", { text: title }));
    nodes.forEach(function (n) { main.appendChild(n); });
  }
  function statCard(label, value) {
    return h("div", { class: "stat" }, [h("div", { class: "k", text: label }), h("div", { class: "v", text: fmt(value) })]);
  }

  // ---------------- overview ----------------
  function viewOverview() {
    var wrap = h("div", { text: "Loading…", class: "muted" });
    setMain("Overview", [wrap]);
    api("GET", "/api/admin/overview").then(function (o) {
      wrap.textContent = "";
      wrap.className = "";
      wrap.appendChild(h("div", { class: "grid" }, [
        statCard("Active users", o.users.active), statCard("Suspended", o.users.suspended),
        statCard("Blocked", o.users.blocked), statCard("Active sessions", o.active_sessions),
        statCard("Requests today", o.today.requests), statCard("Tokens today", o.today.tokens),
        statCard("Requests this month", o.month.requests), statCard("Tokens this month", o.month.tokens),
        statCard("Failed requests (24h)", o.failed_requests_24h), statCard("Website conversations", o.conversations.conversations),
        statCard("Website messages", o.conversations.messages), statCard("Admin memory rows", o.memory.memory_rows),
        statCard("Q&A rows", o.memory.qa_rows),
      ]));
      wrap.appendChild(h("p", { class: "muted small", text: "Daily periods use Asia/Dhaka time." }));
    }).catch(function (e) { wrap.textContent = ""; wrap.appendChild(box("bad", e.message)); fail(e); });
  }

  // ---------------- users ----------------
  var userState = { q: "", status: "", page: 1 };
  function viewUsers(uidFocus) {
    var q = h("input", { type: "text", placeholder: "Search by id, username or name", value: userState.q, "aria-label": "Search users" });
    var status = h("select", { "aria-label": "Status filter" }, [
      h("option", { value: "", text: "All statuses" }), h("option", { value: "active", text: "Active" }),
      h("option", { value: "suspended", text: "Suspended" }), h("option", { value: "blocked", text: "Blocked" }),
    ]);
    status.value = userState.status;
    var results = h("div");
    var detail = h("div");
    function load() {
      var qs = "?page=" + userState.page + "&q=" + encodeURIComponent(userState.q) + "&status=" + encodeURIComponent(userState.status);
      results.textContent = "Loading…";
      api("GET", "/api/admin/users" + qs).then(function (d) {
        results.textContent = "";
        results.appendChild(table(["User", "Status", "Last activity", "Sessions", ""], d.items.map(function (u) {
          var name = [u.first_name, u.last_name].filter(Boolean).join(" ") || "(no name)";
          return [
            { node: h("div", null, [h("strong", { text: name }), h("div", { class: "small muted", text: (u.username ? "@" + u.username + " · " : "") + u.telegram_user_id })]) },
            h("span", { class: "badge " + u.access_status, text: u.access_status }),
            when(u.last_activity_at), String(u.active_sessions),
            button("Manage", function () { openUser(u.telegram_user_id, detail); }),
          ];
        })));
        results.appendChild(pager(userState.page, d.total, function (p) { userState.page = p; load(); }));
      }).catch(function (e) { results.textContent = ""; results.appendChild(box("bad", e.message)); fail(e); });
    }
    var form = h("div", { class: "toolbar" }, [q, status, button("Search", function () {
      userState.q = q.value.trim(); userState.status = status.value; userState.page = 1; load();
    }, "primary")]);
    setMain("Users", [form, results, detail]);
    load();
    if (uidFocus) openUser(uidFocus, detail);
  }

  function openUser(uid, into) {
    into.textContent = "";
    into.appendChild(h("div", { class: "panel", text: "Loading user " + uid + "…" }));
    api("GET", "/api/admin/users/" + encodeURIComponent(uid)).then(function (u) {
      into.textContent = "";
      into.appendChild(userPanel(u, into));
    }).catch(function (e) { into.textContent = ""; into.appendChild(box("bad", e.message)); fail(e); });
  }

  function field(label, input, hint) {
    return h("label", null, [label, input, hint ? h("span", { class: "small muted", text: hint }) : null]);
  }
  function numberInput(value, min, max) {
    return h("input", { type: "number", min: min, max: max, step: 1, value: value === null || value === undefined ? "" : value, placeholder: "default" });
  }

  function userPanel(u, into) {
    var status = h("select", null, [h("option", { value: "active", text: "Active" }),
      h("option", { value: "suspended", text: "Suspended" }), h("option", { value: "blocked", text: "Blocked" })]);
    status.value = u.access_status;
    var dReq = numberInput(u.daily_request_limit, 0, 10000);
    var mReq = numberInput(u.monthly_request_limit, 0, 100000);
    var dTok = numberInput(u.daily_token_limit, 0, 100000000);
    var mTok = numberInput(u.monthly_token_limit, 0, 1000000000);
    var cool = numberInput(u.cooldown_seconds, 0, 600);
    var maxResp = numberInput(u.max_response_tokens, 50, 8000);
    var model = h("input", { type: "text", value: u.model_override || "", placeholder: "default model", maxlength: 80 });
    var notes = h("textarea", { rows: 2, maxlength: 500, text: u.admin_notes || "" });
    var out = h("div");
    function readNum(input) { return input.value.trim() === "" ? null : Number(input.value); }
    function save() {
      var payload = {
        access_status: status.value, daily_request_limit: readNum(dReq), monthly_request_limit: readNum(mReq),
        daily_token_limit: readNum(dTok), monthly_token_limit: readNum(mTok), cooldown_seconds: readNum(cool),
        max_response_tokens: readNum(maxResp), model_override: model.value.trim() || null,
        admin_notes: notes.value.trim() || null,
      };
      if (status.value !== u.access_status && status.value !== "active" &&
          !window.confirm("Change status to " + status.value + "? Active sessions will be signed out.")) return;
      api("PATCH", "/api/admin/users/" + u.telegram_user_id, payload).then(function (r) {
        toast("User updated" + (r.sessions_revoked ? " · " + r.sessions_revoked + " session(s) signed out" : "") + ".");
        openUser(u.telegram_user_id, into);
      }).catch(function (e) {
        out.textContent = "";
        if (e.data && e.data.fields) {
          out.appendChild(box("bad", "Fix: " + Object.keys(e.data.fields).map(function (k) { return k + " " + e.data.fields[k]; }).join("; ")));
        } else { out.appendChild(box("bad", e.message)); }
        fail(e);
      });
    }
    function resetQuota(scope) {
      if (!window.confirm("Reset " + scope + " usage counters for this user?")) return;
      api("POST", "/api/admin/users/" + u.telegram_user_id + "/reset-quota", { scope: scope }).then(function () {
        toast("Counters reset."); openUser(u.telegram_user_id, into);
      }).catch(fail);
    }
    function revoke() {
      if (!window.confirm("Sign this user out of all devices?")) return;
      api("POST", "/api/admin/users/" + u.telegram_user_id + "/revoke-sessions", {}).then(function (r) {
        toast("Signed out " + r.revoked + " session(s)."); openUser(u.telegram_user_id, into);
      }).catch(fail);
    }
    var name = [u.first_name, u.last_name].filter(Boolean).join(" ") || "(no name)";
    var usage = (u.usage || []).map(function (p) {
      return p.period_type + " " + p.period_key + ": " + p.requests + " req, " + p.tokens + " tokens";
    });
    return h("div", { class: "panel" }, [
      h("h3", { text: "User · " + name + " (" + u.telegram_user_id + ")" }),
      h("div", { class: "kv" }, [
        h("div", { text: "Username" }), h("div", { text: fmt(u.username ? "@" + u.username : null) }),
        h("div", { text: "Active sessions" }), h("div", { text: String(u.active_sessions) }),
        h("div", { text: "Conversations" }), h("div", { text: String(u.conversations) }),
        h("div", { text: "Today / month usage" }), h("div", { text: usage.length ? usage.join(" | ") : "no usage yet" }),
        h("div", { text: "Last activity" }), h("div", { text: when(u.last_activity_at) }),
      ]),
      h("div", { class: "form-grid mt" }, [
        field("Status", status), field("Daily requests", dReq, "0 = blocked · empty = default"),
        field("Monthly requests", mReq, "0 = blocked · empty = default"),
        field("Daily tokens", dTok, "0 = unlimited · empty = default"),
        field("Monthly tokens", mTok, "0 = unlimited · empty = default"),
        field("Cooldown (seconds)", cool, "empty = default"), field("Max response tokens", maxResp, "50–8000 · empty = default"),
        field("Model override", model, "empty = server default"), field("Admin notes (private)", notes),
      ]),
      h("div", { class: "actions-row" }, [
        button("Save changes", save, "primary"), button("Reset today's usage", function () { resetQuota("today"); }),
        button("Reset this month", function () { resetQuota("month"); }),
        button("Reset all counters", function () { resetQuota("all"); }, "danger"),
        button("Sign out everywhere", revoke, "danger"),
        button("View conversations", function () { userState.page = 1; window.__adminConvUid = u.telegram_user_id; showTab("conversations"); }),
      ]),
      out,
    ]);
  }

  // ---------------- settings ----------------
  var settingsNotice = null;  // result of the last save; shown once after the panel re-renders
  function viewSettings() {
    var wrap = h("div", { text: "Loading…", class: "muted" });
    setMain("Settings", [wrap]);
    api("GET", "/api/admin/settings").then(function (s) {
      wrap.textContent = "";
      wrap.className = "";
      var inputs = {};
      var initial = {};
      var groups = {};
      s.items.forEach(function (item) { (groups[item.group] = groups[item.group] || []).push(item); });
      var status = h("div");
      var panels = Object.keys(groups).map(function (g) {
        var grid = h("div", { class: "form-grid" });
        groups[g].forEach(function (item) {
          var control;
          if (item.type === "bool") {
            control = h("input", { type: "checkbox", checked: !!item.value });
            inputs[item.key] = control;
            initial[item.key] = !!item.value;
            grid.appendChild(h("label", { class: "check" }, [control, item.label + (item.live ? "" : " (restart required)")]));
            return;
          }
          if (item.type === "choice") {
            control = h("select", null, item.choices.map(function (c) { return h("option", { value: c, text: c }); }));
            control.value = item.value;
          } else if (item.type === "text") {
            control = h("textarea", { rows: 3, maxlength: item.max_len, text: item.value || "" });
          } else {
            control = h("input", { type: "number", min: item.min, max: item.max, step: 1, value: item.value });
          }
          inputs[item.key] = control;
          initial[item.key] = item.type === "int" ? Number(item.value) : (item.value || "");
          grid.appendChild(h("label", null, [item.label + (item.live ? "" : " · restart required"), control,
            h("span", { class: "small muted", text: item.key + (item.min != null ? " · " + item.min + "–" + item.max : "") })]));
        });
        return h("div", { class: "panel" }, [h("h3", { text: g }), grid]);
      });
      function save() {
        var changed = {};
        Object.keys(inputs).forEach(function (k) {
          var c = inputs[k];
          var value = c.type === "checkbox" ? c.checked : (c.type === "number" ? Number(c.value) : c.value);
          if (value !== initial[k]) changed[k] = value;
        });
        if (!Object.keys(changed).length) { toast("No changes to save."); return; }
        api("PUT", "/api/admin/settings", { values: changed }).then(function (r) {
          var parts = [];
          if (r.applied_now.length) parts.push("Applied now: " + r.applied_now.join(", "));
          if (r.restart_required.length) parts.push("Saved; restart the service to apply: " + r.restart_required.join(", "));
          settingsNotice = box(r.restart_required.length ? "info" : "ok", parts.join(" · ") || "Saved.");
          viewSettings();
        }).catch(function (e) {
          status.textContent = "";
          var msgs = e.data && e.data.fields ? Object.keys(e.data.fields).map(function (k) { return k + ": " + e.data.fields[k]; }) : [e.message];
          status.appendChild(box("bad", msgs.join(" · ")));
        });
      }
      var secrets = Object.keys(s.secrets_configured).map(function (k) {
        return h("div", { class: s.secrets_configured[k] ? "tag-ok" : "tag-bad", text: k + ": " + (s.secrets_configured[k] ? "configured" : "missing") });
      });
      var env = Object.keys(s.environment_fields).map(function (k) {
        return h("div", { text: k + ": " + (typeof s.environment_fields[k] === "boolean" ? (s.environment_fields[k] ? "set" : "not set") : fmt(s.environment_fields[k])) });
      });
      var pending = Object.keys(s.pending_restart || {});
      wrap.appendChild(h("p", { class: "muted small", text: "Secrets are never shown. Values below are only what the server reports as configured. Settings marked 'restart required' are stored and take effect after the next restart." }));
      if (pending.length) wrap.appendChild(box("info", "Waiting for a restart: " + pending.join(", ")));
      wrap.appendChild(h("div", { class: "panel" }, [h("h3", { text: "Secrets & environment (read-only)" }),
        h("div", { class: "form-grid" }, [h("div", null, secrets), h("div", null, env)])]));
      panels.forEach(function (p) { wrap.appendChild(p); });
      wrap.appendChild(h("div", { class: "actions-row" }, [button("Save changes", save, "primary"), button("Discard", viewSettings)]));
      wrap.appendChild(status);
      if (settingsNotice) { status.appendChild(settingsNotice); settingsNotice = null; }
    }).catch(function (e) { wrap.textContent = ""; wrap.appendChild(box("bad", e.message)); fail(e); });
  }

  // ---------------- memory ----------------
  var memState = { kind: "memory", q: "", source: "", page: 1 };
  function viewMemory() {
    var kind = h("select", { "aria-label": "Memory type" }, [h("option", { value: "memory", text: "Admin Memory" }), h("option", { value: "qa", text: "Q&A Memory" })]);
    kind.value = memState.kind;
    var q = h("input", { type: "text", placeholder: "Search text", value: memState.q, "aria-label": "Search memory" });
    var source = h("select", { "aria-label": "Source" }, [h("option", { value: "", text: "All sources" }), h("option", { value: "telegram", text: "Telegram" }),
      h("option", { value: "import", text: "Imported" }), h("option", { value: "web", text: "Dashboard" })]);
    source.value = memState.source;
    var results = h("div");
    var editor = h("div");
    var importBox = h("div");
    function load() {
      results.textContent = "Loading…";
      var qs = "?kind=" + memState.kind + "&page=" + memState.page + "&q=" + encodeURIComponent(memState.q) + "&source=" + encodeURIComponent(memState.source);
      api("GET", "/api/admin/memory" + qs).then(function (d) {
        results.textContent = "";
        var cols = memState.kind === "memory" ? ["Text", "Topic", "Source", "Created", ""] : ["Question", "Answer", "Source", "Created", ""];
        results.appendChild(table(cols, d.items.map(function (it) {
          var a = memState.kind === "memory" ? [{ wrap: true, text: it.text }, it.topic, it.source_kind, when(it.created)]
                                             : [{ wrap: true, text: it.question }, { wrap: true, text: it.answer }, it.source_kind, when(it.created)];
          a.push(h("div", { class: "actions-row inline" }, [
            button("Edit", function () { openEditor(it); }),
            button("Delete", function () { removeEntry(it); }, "danger"),
          ]));
          return a;
        })));
        results.appendChild(pager(memState.page, d.total, function (p) { memState.page = p; load(); }));
      }).catch(function (e) { results.textContent = ""; results.appendChild(box("bad", e.message)); fail(e); });
    }
    function openEditor(item) {
      editor.textContent = "";
      var isMem = memState.kind === "memory";
      var t = isMem ? h("textarea", { rows: 3, maxlength: 2000, text: item ? item.text : "" })
                    : h("textarea", { rows: 2, maxlength: 2000, text: item ? item.question : "" });
      var a = isMem ? null : h("textarea", { rows: 3, maxlength: 2000, text: item ? item.answer : "" });
      var msg = h("div");
      var fields = isMem ? [field("Text", t)] : [field("Question", t), field("Answer", a)];
      editor.appendChild(h("div", { class: "panel" }, [
        h("h3", { text: item ? "Edit entry #" + item.id : "Add entry" }),
        h("div", { class: "form-grid" }, fields), msg,
        h("div", { class: "actions-row" }, [button("Save", function () {
          var body = isMem ? { text: t.value } : { question: t.value, answer: a.value };
          var req = item ? api("PATCH", "/api/admin/memory/" + memState.kind + "/" + item.id, body)
                         : api("POST", "/api/admin/memory", Object.assign({ kind: memState.kind }, body));
          req.then(function () { toast(item ? "Entry updated." : "Entry added."); editor.textContent = ""; load(); })
            .catch(function (e) {
              msg.textContent = "";
              msg.appendChild(box("bad", e.data && e.data.fields ? Object.keys(e.data.fields).map(function (k) { return k + ": " + e.data.fields[k]; }).join(" · ") : e.message));
            });
        }, "primary"), button("Cancel", function () { editor.textContent = ""; })]),
      ]));
    }
    function removeEntry(item) {
      if (!window.confirm("Delete this entry permanently?")) return;
      api("DELETE", "/api/admin/memory/" + memState.kind + "/" + item.id).then(function () { toast("Deleted."); load(); }).catch(fail);
    }
    function buildImport() {
      var file = h("input", { type: "file", accept: ".json,.txt,application/json,text/plain", "aria-label": "Upload file" });
      var kindSel = h("select", null, [h("option", { value: "memory", text: "Admin Memory (.json or .txt)" }), h("option", { value: "qa", text: "Q&A (.json)" })]);
      kindSel.value = memState.kind;
      var out = h("div");
      var pending = null;
      function preview() {
        if (!file.files || !file.files[0]) { out.textContent = ""; out.appendChild(box("bad", "Choose a file first.")); return; }
        var fd = new FormData();
        fd.append("kind", kindSel.value);
        fd.append("file", file.files[0]);
        out.textContent = "Checking file…";
        api("POST", "/api/admin/memory/import/preview", fd).then(function (r) {
          pending = r.token;
          var p = r.preview;
          out.textContent = "";
          out.appendChild(box("info", "Records: " + p.total + " · new: " + p.new + " · already stored (skipped): " + p.duplicates));
          if (p.sample.length) out.appendChild(h("pre", { class: "log", text: p.sample.map(function (s) { return JSON.stringify(s); }).join("\n") }));
          out.appendChild(h("div", { class: "actions-row" }, [button("Import " + p.new + " new record(s)", commit, "primary")]));
        }).catch(function (e) { out.textContent = ""; out.appendChild(box("bad", e.message)); fail(e); });
      }
      function commit() {
        api("POST", "/api/admin/memory/import/commit", { token: pending }).then(function (r) {
          out.textContent = "";
          out.appendChild(box("ok", "Imported " + r.imported + " · skipped " + r.duplicates + " · failed " + r.failed + " (batch #" + r.batch_id + ")"));
          pending = null;
          loadBatches();
          load();
        }).catch(function (e) { out.textContent = ""; out.appendChild(box("bad", e.message)); fail(e); });
      }
      var batches = h("div");
      function loadBatches() {
        api("GET", "/api/admin/memory/batches").then(function (d) {
          batches.textContent = "";
          batches.appendChild(table(["#", "Type", "File", "Records", "Imported", "Skipped", "When"], d.items.map(function (b) {
            return [b.id, b.kind, b.file_name || "—", b.total_records, b.imported, b.duplicates, when(b.created_at)];
          })));
        }).catch(fail);
      }
      var dedupe = h("div");
      function checkDupes() {
        api("GET", "/api/admin/memory/dedupe?kind=" + kindSel.value).then(function (d) {
          dedupe.textContent = "";
          dedupe.appendChild(box(d.removable ? "info" : "ok", d.removable
            ? d.removable + " exact duplicate row(s) can be removed. " + d.rule
            : "No exact duplicates found."));
          if (d.groups.length) {
            dedupe.appendChild(table(["Sample", "Copies", "Keep id"], d.groups.map(function (g) {
              return [{ wrap: true, text: g.sample }, g.copies, g.keep_id];
            })));
          }
          if (d.removable) {
            dedupe.appendChild(h("div", { class: "actions-row" }, [button("Remove " + d.removable + " duplicate(s)", function () {
              if (!window.confirm("Remove " + d.removable + " duplicate row(s)? The oldest copy of each is kept.")) return;
              api("POST", "/api/admin/memory/dedupe", { kind: kindSel.value, expected: d.removable }).then(function (r) {
                toast("Removed " + r.removed + " duplicate(s)."); checkDupes(); load();
              }).catch(function (e) { fail(e); checkDupes(); });
            }, "danger")]));
          }
        }).catch(fail);
      }
      loadBatches();
      importBox.textContent = "";
      importBox.appendChild(h("div", { class: "panel" }, [
        h("h3", { text: "Import" }),
        h("div", { class: "toolbar" }, [kindSel, file, button("Preview", preview, "primary")]),
        h("p", { class: "small muted", text: "JSON: an array of {\"text\": \"...\"} or {\"question\": \"...\", \"answer\": \"...\"}. Max 1 MB and 2000 records. Duplicates are skipped." }),
        out,
        h("h3", { text: "Import history" }), batches,
      ]));
      importBox.appendChild(h("div", { class: "panel" }, [
        h("h3", { text: "Duplicates" }),
        h("div", { class: "toolbar" }, [button("Check duplicates", checkDupes)]),
        dedupe,
      ]));
    }
    kind.addEventListener("change", function () { memState.kind = kind.value; memState.page = 1; viewMemory(); });
    var toolbar = h("div", { class: "toolbar" }, [kind, q, source,
      button("Search", function () { memState.q = q.value.trim(); memState.source = source.value; memState.page = 1; load(); }),
      button("Add entry", function () { openEditor(null); }, "primary")]);
    var importToggle = button("Import & duplicates", function () { importBox.classList.toggle("hidden"); buildImport(); });
    setMain("Memory & Q&A", [toolbar, editor, results, h("div", { class: "toolbar mt" }, [importToggle]), importBox]);
    importBox.classList.add("hidden");
    load();
  }

  // ---------------- conversations ----------------
  var convState = { uid: "", page: 1 };
  function viewConversations() {
    if (window.__adminConvUid) { convState.uid = String(window.__adminConvUid); window.__adminConvUid = null; }
    var uid = h("input", { type: "text", placeholder: "Filter by Telegram user id", value: convState.uid, "aria-label": "Telegram user id" });
    var results = h("div");
    var viewer = h("div");
    function load() {
      var qs = "?page=" + convState.page + (convState.uid ? "&uid=" + encodeURIComponent(convState.uid) : "");
      results.textContent = "Loading…";
      api("GET", "/api/admin/conversations" + qs).then(function (d) {
        results.textContent = "";
        results.appendChild(table(["Title", "User", "Messages", "Last activity", ""], d.items.map(function (c) {
          return [{ wrap: true, text: c.title }, (c.username ? "@" + c.username : "") + " " + c.telegram_user_id, c.message_count,
            when(c.last_message_at || c.created_at),
            h("div", { class: "actions-row inline" }, [
              button("View", function () { openTranscript(c.id); }),
              button("Delete", function () { removeConv(c); }, "danger")])];
        })));
        results.appendChild(pager(convState.page, d.total, function (p) { convState.page = p; load(); }));
      }).catch(function (e) { results.textContent = ""; results.appendChild(box("bad", e.message)); fail(e); });
    }
    function openTranscript(id) {
      viewer.textContent = "Loading transcript…";
      api("GET", "/api/admin/conversations/" + id).then(function (t) {
        viewer.textContent = "";
        viewer.appendChild(h("div", { class: "panel" }, [
          h("h3", { text: "Transcript · " + t.title }),
          box("info", "Private website chat. Viewing it is recorded in the audit log. It is not part of Admin Memory."),
          h("div", null, t.messages.map(function (m) {
            var body = h("div", { class: "bubble" });
            if (m.role === "assistant") window.SafeMarkdown.renderInto(document, body, m.content || "");
            else body.textContent = m.content || "";
            return h("div", { class: "msg-block " + m.role }, [h("div", { class: "small muted", text: m.role + " · " + when(m.created_at) + (m.used_memory ? " · used memory" : "") + (m.status !== "complete" ? " · " + m.status : "") }), body]);
          })),
        ]));
      }).catch(function (e) { viewer.textContent = ""; viewer.appendChild(box("bad", e.message)); fail(e); });
    }
    function removeConv(c) {
      if (!window.confirm("Delete this conversation for the user? This cannot be undone.")) return;
      api("DELETE", "/api/admin/conversations/" + c.id).then(function () { toast("Conversation deleted."); viewer.textContent = ""; load(); }).catch(fail);
    }
    setMain("Website conversations", [
      h("div", { class: "toolbar" }, [uid, button("Apply", function () { convState.uid = uid.value.trim(); convState.page = 1; load(); }, "primary"),
        button("Clear", function () { uid.value = ""; convState.uid = ""; convState.page = 1; load(); })]),
      h("p", { class: "small muted", text: "Users' website chats are private. Admins can open a transcript for moderation only; each view is logged." }),
      results, viewer]);
    load();
  }

  // ---------------- usage ----------------
  function viewUsage() {
    var days = h("select", null, [7, 1, 30, 90].map(function (d) { return h("option", { value: d, text: "Last " + d + " day(s)" }); }));
    var out = h("div");
    function load() {
      out.textContent = "Loading…";
      api("GET", "/api/admin/usage?days=" + days.value).then(function (u) {
        out.textContent = "";
        out.appendChild(h("div", { class: "panel" }, [h("h3", { text: "Requests per day and surface" }),
          table(["Day (Dhaka)", "Surface", "Requests", "Tokens", "Errors"], u.daily.map(function (r) {
            return [r.day, r.source, r.requests, r.tokens, r.errors];
          }))]));
        out.appendChild(h("div", { class: "panel" }, [h("h3", { text: "Web request outcomes (ledger)" }),
          table(["Status", "Error type", "Count"], u.ledger.map(function (r) { return [r.status, r.error_type || "—", r.c]; }))]));
        out.appendChild(h("div", { class: "panel" }, [h("h3", { text: "Top users today/period" }),
          table(["User", "Requests", "Tokens"], u.top_users.map(function (r) {
            return [{ node: h("div", null, [h("strong", { text: r.first_name || "(no name)" }), h("div", { class: "small muted", text: (r.username ? "@" + r.username + " · " : "") + r.telegram_user_id })]) }, r.requests, r.tokens];
          }))]));
      }).catch(function (e) { out.textContent = ""; out.appendChild(box("bad", e.message)); fail(e); });
    }
    days.addEventListener("change", load);
    setMain("Usage", [h("div", { class: "toolbar" }, [days, button("Refresh", load)]), out]);
    load();
  }

  // ---------------- audit & logs ----------------
  var auditState = { page: 1 };
  function viewAudit() {
    var audit = h("div");
    var logs = h("div");
    var level = h("select", null, ["INFO", "WARNING", "ERROR"].map(function (l) { return h("option", { value: l, text: l + " and above" }); }));
    function loadAudit() {
      api("GET", "/api/admin/audit?page=" + auditState.page).then(function (d) {
        audit.textContent = "";
        audit.appendChild(table(["When", "Actor", "Action", "Target", "Summary"], d.items.map(function (a) {
          return [when(a.created_at), a.actor_telegram_id, a.action, (a.target_type || "") + (a.target_id ? " " + a.target_id : ""), { wrap: true, text: a.summary }];
        })));
        audit.appendChild(pager(auditState.page, d.total, function (p) { auditState.page = p; loadAudit(); }));
      }).catch(function (e) { audit.textContent = ""; audit.appendChild(box("bad", e.message)); fail(e); });
    }
    function loadLogs() {
      api("GET", "/api/admin/logs?level=" + level.value).then(function (d) {
        logs.textContent = "";
        logs.appendChild(h("p", { class: "small muted", text: d.note }));
        logs.appendChild(h("pre", { class: "log", text: d.items.length ? d.items.map(function (i) {
          return i.time + " " + i.level + " " + i.logger + " " + i.message;
        }).join("\n") : "No log lines at this level." }));
      }).catch(fail);
    }
    level.addEventListener("change", loadLogs);
    setMain("Audit & logs", [
      h("div", { class: "panel" }, [h("h3", { text: "Admin audit trail" }), audit]),
      h("div", { class: "panel" }, [h("h3", { text: "Recent application logs" }), h("div", { class: "toolbar" }, [level, button("Refresh", loadLogs)]), logs]),
    ]);
    loadAudit();
    loadLogs();
  }

  // ---------------- diagnostics ----------------
  function viewDiagnostics() {
    var out = h("div", { text: "Loading…", class: "muted" });
    function ok(v, label) { return h("div", { class: v ? "tag-ok" : "tag-bad", text: label || (v ? "yes" : "no") }); }
    function load() {
      out.textContent = "Loading…";
      api("GET", "/api/admin/diagnostics").then(function (d) {
        out.textContent = "";
        var sec = Object.keys(d.secrets_configured).map(function (k) { return h("div", { class: d.secrets_configured[k] ? "tag-ok" : "tag-bad", text: k + ": " + (d.secrets_configured[k] ? "configured" : "missing") }); });
        var cfg = Object.keys(d.config).map(function (k) { return h("div", { text: k + ": " + (typeof d.config[k] === "boolean" ? (d.config[k] ? "set / on" : "not set / off") : fmt(d.config[k])) }); });
        out.appendChild(h("div", { class: "grid" }, [
          h("div", { class: "panel" }, [h("h3", { text: "Service" }), h("div", { class: "kv" }, [
            h("div", { text: "Version" }), h("div", { text: d.version }),
            h("div", { text: "Environment" }), h("div", { text: d.environment }),
            h("div", { text: "Commit" }), h("div", { text: fmt(d.render_commit) }),
            h("div", { text: "AI" }), h("div", { text: (d.ai.enabled ? "enabled" : "disabled") + " · " + d.ai.model }),
          ])]),
          h("div", { class: "panel" }, [h("h3", { text: "Database" }), h("div", { class: "kv" }, [
            h("div", { text: "Available" }), ok(d.database.available),
            h("div", { text: "Latency" }), h("div", { text: d.database.latency_ms != null ? d.database.latency_ms + " ms" : "—" }),
            h("div", { text: "Server" }), h("div", { text: fmt(d.database.server_version) }),
            h("div", { text: "Missing tables" }), h("div", { text: (d.database.missing_tables || []).join(", ") || "none" }),
          ]), d.database.error ? box("bad", d.database.error) : null]),
          h("div", { class: "panel" }, [h("h3", { text: "Migrations" }), d.migrations.error ? box("bad", d.migrations.error) : h("div", { class: "kv" }, [
            h("div", { text: "Applied" }), h("div", { text: String((d.migrations.applied || []).length) }),
            h("div", { text: "Pending" }), h("div", { text: (d.migrations.pending || []).join(", ") || "none" }),
            h("div", { text: "Tampered" }), h("div", { text: (d.migrations.tampered || []).join(", ") || "none" }),
          ])]),
          h("div", { class: "panel" }, [h("h3", { text: "Telegram" }), d.telegram.ok
            ? h("div", { class: "tag-ok", text: "Bot API reachable · @" + (d.telegram.bot_username || "?") })
            : h("div", { class: "tag-bad", text: "Bot API not reachable (" + d.telegram.reason + ")" })]),
          h("div", { class: "panel" }, [h("h3", { text: "Secrets (presence only)" }), h("div", null, sec)]),
          h("div", { class: "panel" }, [h("h3", { text: "Configuration (presence only)" }), h("div", null, cfg)]),
          h("div", { class: "panel" }, [h("h3", { text: "Problems" }), d.problems.length ? h("ul", null, d.problems.map(function (p) { return h("li", { text: p }); })) : h("div", { class: "tag-ok", text: "none" })]),
          h("div", { class: "panel" }, [h("h3", { text: "Warnings" }), d.warnings.length ? h("ul", null, d.warnings.map(function (p) { return h("li", { text: p }); })) : h("div", { class: "tag-ok", text: "none" })]),
        ]));
      }).catch(function (e) { out.textContent = ""; out.appendChild(box("bad", e.message)); fail(e); });
    }
    setMain("Diagnostics", [h("div", { class: "toolbar" }, [button("Refresh", load)]), out]);
    load();
  }

  // ---------------- navigation ----------------
  var views = {
    overview: viewOverview, users: function () { viewUsers(null); }, settings: viewSettings, memory: viewMemory,
    conversations: viewConversations, usage: viewUsage, audit: viewAudit, diagnostics: viewDiagnostics,
  };
  function showTab(tab) {
    if (!views[tab]) tab = "overview";
    document.querySelectorAll(".admin-nav button[data-tab]").forEach(function (b) {
      if (b.getAttribute("data-tab") === tab) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    if (window.location.hash !== "#" + tab) history.replaceState(null, "", "#" + tab);
    views[tab]();
  }
  document.querySelectorAll(".admin-nav button[data-tab]").forEach(function (b) {
    b.addEventListener("click", function () { showTab(b.getAttribute("data-tab")); });
  });
  document.getElementById("logout").addEventListener("click", function () {
    api("POST", "/api/auth/logout", {}).catch(function () {}).then(function () { window.location.assign("/login"); });
  });

  api("GET", "/api/auth/me").then(function (me) {
    window.IGCommon.setCsrf(me.csrf_token);
    if (me.user.role !== "admin") { window.location.assign("/"); return; }
    showTab((window.location.hash || "#overview").slice(1));
  }).catch(function (e) {
    if (isAuthLost(e) || e.status === 401) { window.location.assign("/login?next=/admin_panel"); return; }
    setMain("Admin", [box("bad", e.message || "Could not load the admin panel.")]);
  });
})();
