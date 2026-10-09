/* Shared helpers for the website: JSON API calls with CSRF, toasts, request ids. */
(function (window) {
  "use strict";
  var state = { csrf: "" };

  function api(method, url, body) {
    var headers = { Accept: "application/json" };
    var payload;
    if (body instanceof FormData) {
      payload = body;
    } else if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    if (method !== "GET" && method !== "HEAD") headers["X-CSRF-Token"] = state.csrf;
    return fetch(url, {
      method: method, headers: headers, body: payload, credentials: "same-origin", cache: "no-store",
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) {
          var err = new Error((data && data.error) || ("Request failed (" + res.status + ")"));
          err.status = res.status;
          err.code = data && data.code;
          err.retryAfter = data && data.retry_after;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  function toast(message, ms) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.classList.add("hidden"); }, ms || 2600);
  }

  function requestId() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID().replace(/-/g, "");
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.prototype.map.call(bytes, function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
  }

  function setCsrf(token) { state.csrf = token || ""; }

  // Sends the user to the login page when the session is gone or revoked.
  function isAuthLost(err) {
    return err && (err.status === 401 || err.code === "not_member" ||
      (typeof err.code === "string" && err.code.indexOf("access_") === 0));
  }

  window.IGCommon = { api: api, toast: toast, requestId: requestId, setCsrf: setCsrf, isAuthLost: isAuthLost };
})(window);
