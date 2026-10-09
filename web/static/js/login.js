/* Telegram Login Widget in redirect mode. The widget sends the browser to
   /api/auth/telegram/callback with the signed fields; the server verifies them and
   sets the session cookie. No inline or eval-based callbacks: the CSP forbids them. */
(function () {
  "use strict";
  var box = document.getElementById("widget");
  var status = document.getElementById("status");
  var bot = box ? box.getAttribute("data-bot") : "";
  var authUrl = box ? box.getAttribute("data-auth-url") : "";

  function show(message, bad) {
    if (!status) return;
    status.textContent = message;
    status.className = "notice" + (bad ? " bad" : "");
  }

  if (!box || !bot) {
    show("Telegram login is not configured yet.", true);
    return;
  }
  if (!/^[A-Za-z0-9_]{5,64}$/.test(bot)) {
    show("The configured Telegram bot username is invalid.", true);
    return;
  }
  if (!authUrl || authUrl.charAt(0) !== "/" || authUrl.indexOf("//") === 0) {
    show("Login is not set up correctly. Reload the page and try again.", true);
    return;
  }
  var script = document.createElement("script");
  script.async = true;
  script.src = "https://telegram.org/js/telegram-widget.js?22";
  script.setAttribute("data-telegram-login", bot);
  script.setAttribute("data-size", "large");
  script.setAttribute("data-radius", "10");
  script.setAttribute("data-auth-url", authUrl);
  script.setAttribute("data-request-access", "write");
  script.onerror = function () { show("Could not load the Telegram login button. Check your connection.", true); };
  box.appendChild(script);
})();
