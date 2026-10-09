/* Chat website: conversations, messages, copy/regenerate, quota display.
   All server text is inserted with textContent or the safe markdown renderer. */
(function () {
  "use strict";
  var api = window.IGCommon.api;
  var toast = window.IGCommon.toast;
  var requestId = window.IGCommon.requestId;
  var isAuthLost = window.IGCommon.isAuthLost;
  var $ = function (id) { return document.getElementById(id); };
  var els = {
    list: $("conv-list"), inner: $("messages-inner"), messages: $("messages"), input: $("input"),
    send: $("send"), count: $("count"), title: $("conv-title"), quota: $("quota"), newChat: $("new-chat"),
    logout: $("logout"), composer: $("composer"), sidebar: $("sidebar"), scrim: $("scrim"),
    menu: $("menu-btn"), avatar: $("avatar"), hint: $("hint"),
  };
  var state = { activeId: null, conversations: [], busy: false, maxChars: 2000, user: null, lastAssistantId: null };

  function fail(err) {
    if (isAuthLost(err)) { window.location.assign("/login"); return; }
    toast(err.message || "Something went wrong.");
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function initials(name) {
    var t = (name || "?").trim();
    return t ? t.charAt(0).toUpperCase() : "?";
  }

  function fmtTime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  // ---------------- quota ----------------
  function bar(used, limit) {
    var wrap = el("div", "bar");
    var fill = el("span");
    var pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0;
    fill.style.width = (limit ? pct : 0) + "%";
    if (limit && used >= limit) fill.className = "full";
    else if (limit && pct >= 80) fill.className = "warn";
    wrap.appendChild(fill);
    return wrap;
  }

  function row(label, value) {
    var r = el("div", "row");
    r.appendChild(el("span", null, label));
    r.appendChild(el("strong", null, value));
    return r;
  }

  function renderQuota(q) {
    els.quota.textContent = "";
    if (!q) { els.quota.appendChild(el("div", "muted", "Usage is unavailable right now.")); return; }
    var period = function (label, p) {
      var box = el("div");
      box.appendChild(row(label + " requests", p.requests_limit == null ? used(p.requests_used) :
        p.requests_used + " / " + p.requests_limit));
      if (p.requests_limit != null) box.appendChild(bar(p.requests_used, p.requests_limit));
      if (p.tokens_limit) {
        box.appendChild(row(label + " tokens", p.tokens_used + " / " + p.tokens_limit));
      }
      return box;
    };
    els.quota.appendChild(period("Today", q.daily));
    els.quota.appendChild(period("This month", q.monthly));
    if (q.cooldown_remaining_seconds > 0) {
      els.quota.appendChild(el("div", "small", "Next message in " + q.cooldown_remaining_seconds + "s"));
    }
    if (q.daily && q.daily.resets_at) {
      els.quota.appendChild(el("div", "small", "Resets daily at 00:00 Asia/Dhaka"));
    }
  }
  function used(n) { return String(n) + " used"; }

  function refreshQuota() {
    return api("GET", "/api/quota").then(function (q) { renderQuota(q); }).catch(function () {});
  }

  // ---------------- conversations ----------------
  function loadConversations() {
    return api("GET", "/api/conversations").then(function (data) {
      state.conversations = data.items || [];
      renderList();
    });
  }

  function renderList() {
    els.list.textContent = "";
    if (!state.conversations.length) {
      els.list.appendChild(el("li", "muted small", "No conversations yet."));
      return;
    }
    state.conversations.forEach(function (c) {
      var li = el("li", "conv-item" + (c.id === state.activeId ? " active" : ""));
      li.tabIndex = 0;
      li.setAttribute("data-id", c.id);
      var title = el("span", "title", c.title || "New chat");
      title.title = c.title || "New chat";
      li.appendChild(title);
      var actions = el("span", "actions");
      var rename = el("button", "ghost", "✎");
      rename.type = "button";
      rename.setAttribute("aria-label", "Rename conversation");
      rename.addEventListener("click", function (e) { e.stopPropagation(); startRename(li, c); });
      var del = el("button", "ghost danger", "🗑");
      del.type = "button";
      del.setAttribute("aria-label", "Delete conversation");
      del.addEventListener("click", function (e) { e.stopPropagation(); removeConversation(c); });
      actions.appendChild(rename);
      actions.appendChild(del);
      li.appendChild(actions);
      li.addEventListener("click", function () { openConversation(c.id); });
      li.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && e.target === li) openConversation(c.id);
      });
      els.list.appendChild(li);
    });
  }

  function startRename(li, c) {
    var titleEl = li.querySelector(".title");
    var input = el("input");
    input.type = "text";
    input.maxLength = 120;
    input.value = c.title || "";
    input.setAttribute("aria-label", "Conversation title");
    li.replaceChild(input, titleEl);
    input.focus();
    input.select();
    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      var value = input.value.trim();
      if (!save || !value || value === c.title) { renderList(); return; }
      api("PATCH", "/api/conversations/" + c.id, { title: value }).then(function (updated) {
        c.title = updated.title;
        if (state.activeId === c.id) els.title.textContent = updated.title;
        renderList();
      }).catch(function (err) { fail(err); renderList(); });
    }
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", function () { finish(true); });
    input.addEventListener("click", function (e) { e.stopPropagation(); });
  }

  function removeConversation(c) {
    if (!window.confirm("Delete \"" + (c.title || "New chat") + "\"? This cannot be undone.")) return;
    api("DELETE", "/api/conversations/" + c.id).then(function () {
      state.conversations = state.conversations.filter(function (x) { return x.id !== c.id; });
      if (state.activeId === c.id) showEmpty();
      renderList();
      toast("Conversation deleted.");
    }).catch(fail);
  }

  function newChat() {
    if (state.busy) return;
    showEmpty();
    closeSidebar();
    els.input.focus();
  }

  function createConversation() {
    return api("POST", "/api/conversations", {}).then(function (c) {
      state.conversations.unshift({ id: c.id, title: c.title, message_count: 0, last_message_at: null });
      state.activeId = c.id;
      renderList();
      return c;
    });
  }

  function openConversation(id) {
    if (state.busy) return;
    closeSidebar();
    api("GET", "/api/conversations/" + id).then(function (c) {
      state.activeId = c.id;
      els.title.textContent = c.title || "New chat";
      renderMessages(c.messages || []);
      renderList();
    }).catch(fail);
  }

  function showEmpty() {
    state.activeId = null;
    state.lastAssistantId = null;
    els.title.textContent = "New chat";
    els.inner.textContent = "";
    var box = el("div", "empty");
    box.appendChild(el("h3", null, "Ask about the INFO GROUP"));
    box.appendChild(el("p", null, "Your conversation is private. It is never added to the group's memory."));
    els.inner.appendChild(box);
    renderList();
  }

  // ---------------- messages ----------------
  function renderMessages(messages) {
    els.inner.textContent = "";
    state.lastAssistantId = null;
    if (!messages.length) { showEmpty(); return; }
    messages.forEach(function (m) {
      if (m.role === "assistant") state.lastAssistantId = m.id;
      els.inner.appendChild(messageNode(m));
    });
    scrollEnd();
  }

  function scrollEnd() {
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function messageNode(m, extra) {
    var isUser = m.role === "user";
    var status = m.status || "complete";
    var node = el("div", "msg " + (isUser ? "user" : "assistant") + (status === "error" ? " error" : ""));
    node.setAttribute("data-id", m.id === undefined ? "" : String(m.id));
    node.appendChild(el("div", "msg-tag", isUser ? initials(state.user && state.user.display_name) : "AI"));
    var column = el("div");
    column.style.minWidth = "0";
    column.style.maxWidth = "100%";
    var bubble = el("div", "bubble");
    if (isUser) {
      bubble.textContent = m.content || "";
    } else {
      window.SafeMarkdown.renderInto(document, bubble, m.content || "");
    }
    column.appendChild(bubble);
    var meta = el("div", "msg-meta");
    var stamp = fmtTime(m.created_at);
    if (stamp) meta.appendChild(el("span", null, stamp));
    if (!isUser && m.used_memory) meta.appendChild(el("span", "badge memory", "Group memory"));
    if (!isUser && status === "fallback") meta.appendChild(el("span", "badge fallback", "Memory only"));
    if (!isUser && status === "error") meta.appendChild(el("span", "badge error", "Not answered"));
    if (meta.childNodes.length) column.appendChild(meta);
    var actions = el("div", "msg-actions");
    if (m.content) {
      var copy = el("button", "ghost", "Copy");
      copy.type = "button";
      copy.setAttribute("aria-label", "Copy message");
      copy.addEventListener("click", function () { copyText(m.content); });
      actions.appendChild(copy);
    }
    if (!isUser && m.id !== undefined && m.id === state.lastAssistantId) {
      // Always offered for the latest answer; setBusy() disables it while a request is running.
      var regen = el("button", "ghost", "Regenerate");
      regen.type = "button";
      regen.disabled = state.busy;
      regen.setAttribute("aria-label", "Regenerate answer");
      regen.addEventListener("click", regenerate);
      actions.appendChild(regen);
    }
    if (!isUser && status === "error" && extra && extra.retry) {
      var retry = el("button", "ghost", "Retry");
      retry.type = "button";
      retry.addEventListener("click", extra.retry);
      actions.appendChild(retry);
    }
    if (actions.childNodes.length) column.appendChild(actions);
    node.appendChild(column);
    return node;
  }

  function typingNode() {
    var node = el("div", "msg assistant");
    node.id = "typing";
    node.appendChild(el("div", "msg-tag", "AI"));
    var bubble = el("div", "bubble");
    var dots = el("span", "typing");
    dots.setAttribute("aria-label", "Assistant is typing");
    dots.appendChild(el("span")); dots.appendChild(el("span")); dots.appendChild(el("span"));
    bubble.appendChild(dots);
    node.appendChild(bubble);
    return node;
  }

  function copyText(text) {
    var done = function () { toast("Copied."); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
    } else {
      fallbackCopy(text);
      done();
    }
  }

  function fallbackCopy(text) {
    var ta = el("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* ignore: toast still shown */ }
    document.body.removeChild(ta);
  }

  // ---------------- sending ----------------
  function setBusy(flag) {
    state.busy = flag;
    els.send.disabled = flag;
    els.input.disabled = flag;
    document.querySelectorAll("button[aria-label='Regenerate answer']").forEach(function (b) { b.disabled = flag; });
    if (!flag) els.input.focus();
  }

  function appendErrorBubble(message, retry) {
    var node = messageNode({ role: "assistant", content: message, status: "error" }, { retry: retry });
    els.inner.appendChild(node);
    scrollEnd();
  }

  function sendText(text, opts) {
    if (state.busy) return;
    var clean = (text || "").trim();
    if (!clean) return;
    if (clean.length > state.maxChars) { toast("Message is too long (max " + state.maxChars + " characters)."); return; }
    setBusy(true);
    var ensure = state.activeId ? Promise.resolve({ id: state.activeId }) : createConversation();
    var pendingUser = null;
    ensure.then(function (conv) {
      if (!(opts && opts.retry)) {
        pendingUser = messageNode({ role: "user", content: clean, created_at: new Date().toISOString() });
        els.inner.querySelectorAll(".empty").forEach(function (n) { n.remove(); });
        els.inner.appendChild(pendingUser);
      }
      els.inner.appendChild(typingNode());
      scrollEnd();
      return api("POST", "/api/conversations/" + conv.id + "/messages",
                 { content: clean, request_id: requestId() });
    }).then(function (data) {
      var typing = $("typing");
      if (typing) typing.remove();
      if (pendingUser && data.user_message) pendingUser.replaceWith(messageNode(data.user_message));
      if (data.assistant_message) {
        state.lastAssistantId = data.assistant_message.id;
        var answer = data.assistant_message;
        if (answer.status === "error") {
          els.inner.appendChild(messageNode(answer, { retry: function () { sendText(clean, { retry: true }); } }));
        } else {
          els.inner.appendChild(messageNode(answer));
        }
      }
      if (data.quota) renderQuota(data.quota);
      else refreshQuota();
      scrollEnd();
      if (!data.ok) toast("The assistant could not answer this time. Your quota was not charged.");
      loadConversations().catch(function () {});
    }).catch(function (err) {
      var typing = $("typing");
      if (typing) typing.remove();
      if (isAuthLost(err)) { window.location.assign("/login"); return; }
      if (err.status === 429 && err.retryAfter) toast((err.message || "Limit reached.") + " Wait " + err.retryAfter + "s.", 4000);
      if (pendingUser && !pendingUser.parentNode) els.inner.appendChild(pendingUser);
      appendErrorBubble(err.message || "Could not send your message.", function () {
        sendText(clean, { retry: true });
      });
      refreshQuota();
    }).then(function () { setBusy(false); }, function () { setBusy(false); });
  }

  function regenerate() {
    if (state.busy || !state.activeId || !state.lastAssistantId) return;
    var last = els.inner.querySelector(".msg.assistant[data-id='" + state.lastAssistantId + "']");
    setBusy(true);
    if (last) last.replaceWith(typingNode()); else els.inner.appendChild(typingNode());
    api("POST", "/api/conversations/" + state.activeId + "/regenerate", { request_id: requestId() }).then(function (data) {
      var typing = $("typing");
      if (typing) typing.remove();
      if (data.assistant_message) {
        state.lastAssistantId = data.assistant_message.id;
        els.inner.appendChild(messageNode(data.assistant_message));
      }
      if (data.quota) renderQuota(data.quota);
      scrollEnd();
    }).catch(function (err) {
      var typing = $("typing");
      if (typing) typing.remove();
      if (isAuthLost(err)) { window.location.assign("/login"); return; }
      toast(err.message || "Could not regenerate the answer.");
      openConversation(state.activeId);
    }).then(function () { setBusy(false); }, function () { setBusy(false); });
  }

  // ---------------- composer ----------------
  function resize() {
    els.input.style.height = "auto";
    els.input.style.height = Math.min(180, els.input.scrollHeight) + "px";
  }

  els.input.addEventListener("input", function () {
    els.count.textContent = els.input.value.length + " / " + state.maxChars;
    resize();
  });
  els.input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      els.composer.dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });
  els.composer.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = els.input.value;
    if (!text.trim() || state.busy) return;
    els.input.value = "";
    els.count.textContent = "0 / " + state.maxChars;
    resize();
    sendText(text);
  });

  // ---------------- sidebar / session ----------------
  function openSidebar() {
    els.sidebar.classList.add("open");
    els.scrim.classList.add("open");
    els.menu.setAttribute("aria-expanded", "true");
  }
  function closeSidebar() {
    els.sidebar.classList.remove("open");
    els.scrim.classList.remove("open");
    els.menu.setAttribute("aria-expanded", "false");
  }
  els.menu.addEventListener("click", function () {
    if (els.sidebar.classList.contains("open")) closeSidebar(); else openSidebar();
  });
  els.scrim.addEventListener("click", closeSidebar);
  els.newChat.addEventListener("click", newChat);
  els.logout.addEventListener("click", function () {
    api("POST", "/api/auth/logout", {}).catch(function () {}).then(function () {
      window.location.assign("/login");
    });
  });

  function init() {
    api("GET", "/api/auth/me").then(function (me) {
      window.IGCommon.setCsrf(me.csrf_token);
      state.user = me.user;
      state.maxChars = (me.features && me.features.max_message_chars) || 2000;
      els.input.maxLength = state.maxChars;
      els.count.textContent = "0 / " + state.maxChars;
      if (me.user.photo_url) {
        var img = el("img");
        img.src = me.user.photo_url;
        img.alt = "";
        img.referrerPolicy = "no-referrer";
        img.loading = "lazy";
        els.avatar.textContent = "";
        els.avatar.appendChild(img);
      } else {
        els.avatar.textContent = initials(me.user.display_name);
      }
      if (me.features && !me.features.chat_enabled) {
        els.hint.textContent = "AI chat is temporarily disabled by the administrator.";
        els.input.disabled = true;
        els.send.disabled = true;
      }
      renderQuota(me.quota);
      return loadConversations();
    }).then(function () {
      if (!state.conversations.length) showEmpty();
      else openConversation(state.conversations[0].id);
    }).catch(function (err) {
      if (isAuthLost(err) || err.status === 401) { window.location.assign("/login"); return; }
      showEmpty();
      toast(err.message || "Could not load your chats.", 5000);
    });
  }

  init();
})();
