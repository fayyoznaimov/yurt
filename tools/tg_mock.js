/* Подмена Telegram.WebApp для проверки Mini App без настоящего Telegram. Только для тестов, на сайт не попадает.
   Подключение: python tools/serve_test.py, затем открыть http://127.0.0.1:8800/?tgmock=pre&reducedmotion=1 —
   сервер вставляет этот файл (/__tg/mock.js) первым скриптом страницы при ?tgmock=<режим>.
   Проверки параметров — как в telegram-web-app.js (неверный цвет, длинный текст, чужой хост в openTelegramLink —
   исключение), чтобы ошибки использования API были видны и с подменой.
   Режимы (?tgmock=…):
     pre   — WebApp есть сразу (как будто скрипт Telegram уже загружен); initData — из параметров ниже
     late  — WebApp появляется через 700 мс вместо настоящего telegram-web-app.js, данные берёт из
             sessionStorage __telegram__initParams (как настоящий скрипт после чистки адреса)
     fail  — скрипт Telegram не загрузился (onerror через 300 мс)
     empty — WebApp есть, но initData пустой (настоящий скрипт в обычном браузере)
   Параметры: tgscheme=dark|light, tgsp=p_КОД (start_param), tgver=8.0, tgname=Имя, tgvh=600,
   tgnoss=1 (без sessionStorage, как в приватном режиме).
   Записи: window.__tg = { calls: [...], main: {...}, back: {...}, colors: {...}, haptics: [], links: [] };
   window.__tg.pressMain(), pressBack(), setScheme('dark'|'light'), setViewport(h). */
(function () {
  var q = new URLSearchParams(location.search);
  var mode = q.get("tgmock") || "pre";
  var R = window.__tg = { mode: mode, calls: [], main: { visible: false, text: "", active: true, progress: false, color: "", text_color: "" },
    back: { visible: false }, colors: {}, haptics: [], links: [], ready: 0, expand: 0, handlers: {}, errors: [] };
  function log(name, arg) { R.calls.push([name, arg === undefined ? null : arg, Math.round(performance.now())]); }
  function verAtLeast(v, min) {
    var a = String(v).split("."), b = String(min).split(".");
    for (var i = 0; i < Math.max(a.length, b.length); i++) {
      var x = parseInt(a[i] || "0", 10), y = parseInt(b[i] || "0", 10);
      if (x !== y) return x > y;
    }
    return true;
  }
  function hex(c) {
    c = String(c || "");
    if (/^#[0-9a-f]{6}$/i.test(c)) return c.toLowerCase();
    if (/^#[0-9a-f]{3}$/i.test(c)) return ("#" + c[1] + c[1] + c[2] + c[2] + c[3] + c[3]).toLowerCase();
    return false;
  }
  function fail(msg) { R.errors.push(msg); console.error("[mock Telegram.WebApp] " + msg); throw new Error(msg); }
  function qsParse(s) {
    var o = {};
    String(s || "").split("&").forEach(function (kv) {
      if (!kv) return;
      var i = kv.indexOf("="), k = decodeURIComponent(i >= 0 ? kv.slice(0, i) : kv), v = i >= 0 ? decodeURIComponent(kv.slice(i + 1).replace(/\+/g, "%20")) : "";
      if (/^[\[{]/.test(v)) { try { v = JSON.parse(v); } catch (e) { /* как есть */ } }
      o[k] = v;
    });
    return o;
  }
  function build(initData, ver, scheme, theme) {
    var unsafe = qsParse(initData);
    var vh = +(q.get("tgvh") || 0) || window.innerHeight;
    var W = {
      initData: initData, initDataUnsafe: unsafe, version: ver, platform: "mock", colorScheme: scheme,
      themeParams: theme || (scheme === "dark" ? { bg_color: "#17212b", text_color: "#f5f5f5" } : { bg_color: "#ffffff", text_color: "#000000" }),
      viewportHeight: vh, viewportStableHeight: vh, isExpanded: false,
      isVersionAtLeast: function (v) { return verAtLeast(ver, v); },
      ready: function () { R.ready++; log("ready"); },
      expand: function () { R.expand++; W.isExpanded = true; log("expand"); },
      close: function () { log("close"); },
      onEvent: function (t, fn) { (R.handlers[t] = R.handlers[t] || []).push(fn); log("onEvent", t); },
      offEvent: function (t, fn) { R.handlers[t] = (R.handlers[t] || []).filter(function (f) { return f !== fn; }); },
      setHeaderColor: function (c) {
        if (!verAtLeast(ver, "6.1")) { console.warn("[mock] header color not supported"); return; }
        if (c === "bg_color" || c === "secondary_bg_color") { R.colors.header = c; log("setHeaderColor", c); return; }
        if (!verAtLeast(ver, "6.9")) fail("Header color key should be bg_color/secondary_bg_color in " + ver);
        if (!hex(c)) fail("Header color format is invalid " + c);
        R.colors.header = hex(c); log("setHeaderColor", c);
      },
      setBackgroundColor: function (c) {
        if (!verAtLeast(ver, "6.1")) { console.warn("[mock] bg color not supported"); return; }
        if (!hex(c)) fail("Background color format is invalid " + c);
        R.colors.bg = hex(c); log("setBackgroundColor", c);
      },
      setBottomBarColor: function (c) {
        if (!verAtLeast(ver, "7.10")) { console.warn("[mock] bottom bar color not supported"); return; }
        if (!hex(c)) fail("Bottom bar color format is invalid " + c);
        R.colors.bottom = hex(c); log("setBottomBarColor", c);
      },
      openTelegramLink: function (url) {
        var a = document.createElement("a"); a.href = url;
        if (a.protocol !== "https:" && a.protocol !== "http:") fail("Url protocol is not supported " + url);
        if (a.hostname !== "t.me") fail("Url host is not supported " + url);
        R.links.push(["tg", url]); log("openTelegramLink", url);
      },
      openLink: function (url) { R.links.push(["ext", url]); log("openLink", url); },
      MainButton: {
        text: "", color: "#2481cc", textColor: "#ffffff", isVisible: false, isActive: true, isProgressVisible: false, _cb: [],
        setText: function (t) { return this.setParams({ text: t }); },
        setParams: function (p) {
          if (p.text !== undefined) {
            var t = String(p.text).trim();
            if (!t.length) fail("Bottom button text is required");
            if (t.length > 64) fail("Bottom button text is too long " + t);
            this.text = t; R.main.text = t;
          }
          if (p.color !== undefined) { if (!hex(p.color)) fail("Bottom button color format is invalid " + p.color); this.color = hex(p.color); R.main.color = this.color; }
          if (p.text_color !== undefined) { if (!hex(p.text_color)) fail("Bottom button text color format is invalid " + p.text_color); this.textColor = hex(p.text_color); R.main.text_color = this.textColor; }
          if (p.is_visible !== undefined) { if (p.is_visible && !this.text) fail("Bottom button text is required"); this.isVisible = !!p.is_visible; R.main.visible = this.isVisible; }
          if (p.is_active !== undefined) { this.isActive = !!p.is_active; R.main.active = this.isActive; }
          log("MainButton.setParams", JSON.stringify(p));
          return this;
        },
        show: function () { if (!this.text) fail("Bottom button text is required"); this.isVisible = true; R.main.visible = true; log("MainButton.show"); return this; },
        hide: function () { this.isVisible = false; R.main.visible = false; log("MainButton.hide"); return this; },
        enable: function () { this.isActive = true; R.main.active = true; return this; },
        disable: function () { this.isActive = false; R.main.active = false; return this; },
        showProgress: function () { this.isProgressVisible = true; R.main.progress = true; log("MainButton.showProgress"); return this; },
        hideProgress: function () { this.isProgressVisible = false; R.main.progress = false; log("MainButton.hideProgress"); return this; },
        onClick: function (fn) { this._cb.push(fn); log("MainButton.onClick"); return this; },
        offClick: function (fn) { this._cb = this._cb.filter(function (f) { return f !== fn; }); return this; }
      },
      BackButton: {
        isVisible: false, _cb: [],
        show: function () { if (!verAtLeast(ver, "6.1")) { console.warn("[mock] BackButton not supported"); return this; } this.isVisible = true; R.back.visible = true; log("BackButton.show"); return this; },
        hide: function () { this.isVisible = false; R.back.visible = false; log("BackButton.hide"); return this; },
        onClick: function (fn) { this._cb.push(fn); log("BackButton.onClick"); return this; },
        offClick: function (fn) { this._cb = this._cb.filter(function (f) { return f !== fn; }); return this; }
      },
      HapticFeedback: {
        impactOccurred: function (s) { if (["light", "medium", "heavy", "rigid", "soft"].indexOf(s) < 0) fail("Haptic impact style is invalid " + s); R.haptics.push("impact:" + s); log("haptic", "impact:" + s); return this; },
        notificationOccurred: function (t) { if (["error", "success", "warning"].indexOf(t) < 0) fail("Haptic notification type is invalid " + t); R.haptics.push("notify:" + t); log("haptic", "notify:" + t); return this; },
        selectionChanged: function () { R.haptics.push("selection"); return this; }
      }
    };
    return W;
  }
  function fire(t, arg) { (R.handlers[t] || []).forEach(function (fn) { fn.call(window.Telegram.WebApp, arg); }); }
  R.pressMain = function () { var B = window.Telegram.WebApp.MainButton; if (!B.isVisible || !B.isActive) return false; B._cb.forEach(function (fn) { fn(); }); return true; };
  R.pressBack = function () { var B = window.Telegram.WebApp.BackButton; if (!B.isVisible) return false; B._cb.forEach(function (fn) { fn(); }); return true; };
  R.setScheme = function (s) { var W = window.Telegram.WebApp; W.colorScheme = s; W.themeParams = s === "dark" ? { bg_color: "#17212b" } : { bg_color: "#ffffff" }; fire("themeChanged"); };
  R.setViewport = function (h) { var W = window.Telegram.WebApp; W.viewportHeight = h; W.viewportStableHeight = h; fire("viewportChanged", { isStateStable: true }); };

  // tgnoss=1 — sessionStorage не пишется (приватный режим / запрет): скрипт в <head> не должен чистить адрес
  if (q.get("tgnoss") === "1") {
    Storage.prototype.setItem = function () { throw new Error("QuotaExceededError (подмена)"); };
  }
  var ver = q.get("tgver") || "8.0", scheme = q.get("tgscheme") || "light";
  var name = q.get("tgname") || "Азиз";
  function fakeInitData() {
    var user = JSON.stringify({ id: 1000001, first_name: name, last_name: "", username: "aziz_test", language_code: "ru" });
    var parts = ["query_id=AAHtest", "user=" + encodeURIComponent(user), "auth_date=" + Math.floor(Date.now() / 1000)];
    if (q.get("tgsp")) parts.push("start_param=" + encodeURIComponent(q.get("tgsp")));
    parts.push("hash=0000000000000000000000000000000000000000000000000000000000000000");
    return parts.join("&");
  }
  if (mode === "pre" || mode === "empty") {
    window.Telegram = { WebApp: build(mode === "empty" ? "" : fakeInitData(), ver, scheme) };
    return;
  }
  // late / fail: перехватить подключение telegram-web-app.js
  var orig = Node.prototype.appendChild;
  Node.prototype.appendChild = function (el) {
    if (el && el.tagName === "SCRIPT" && /telegram\.org\/js\/telegram-web-app\.js/.test(el.src || "")) {
      R.sdkRequested = el.src;
      log("sdk-requested", el.src);
      setTimeout(function () {
        if (mode === "fail") { if (el.onerror) el.onerror({ type: "error" }); return; }
        // как telegram-web-app.js: параметры из hash адреса, недостающие — из sessionStorage
        var p = {}, h = location.hash.replace(/^#/, ""), qi = h.indexOf("?");
        if (qi >= 0) h = h.slice(qi + 1); else if (h.indexOf("=") < 0) h = "";
        h.split("&").forEach(function (kv) {
          if (!kv) return;
          var part = kv.split("=");
          try { p[decodeURIComponent(part[0])] = part[1] == null ? null : decodeURIComponent(part[1].replace(/\+/g, "%20")); } catch (e) { /* пропуск */ }
        });
        R.fromHash = Object.keys(p).filter(function (k) { return k.indexOf("tgWebApp") === 0; });
        var st = {};
        try { st = JSON.parse(sessionStorage.getItem("__telegram__initParams") || "{}") || {}; } catch (e) { st = {}; }
        for (var k in st) if (p[k] === undefined) p[k] = st[k];
        var theme = null;
        try { theme = JSON.parse(p.tgWebAppThemeParams || "null"); } catch (e) { theme = null; }
        var bg = theme && theme.bg_color ? theme.bg_color.replace("#", "") : "ffffff";
        var r = parseInt(bg.substr(0, 2), 16), g = parseInt(bg.substr(2, 2), 16), b = parseInt(bg.substr(4, 2), 16);
        var sc = Math.sqrt(0.299 * r * r + 0.587 * g * g + 0.114 * b * b) < 120 ? "dark" : "light";
        window.Telegram = { WebApp: build(p.tgWebAppData || "", p.tgWebAppVersion || "6.0", sc, theme) };
        R.fromStorage = p;
        if (el.onload) el.onload({ type: "load" });
      }, mode === "fail" ? 300 : 700);
      return el;                      // в документ не добавляем — настоящий скрипт не грузится
    }
    return orig.call(this, el);
  };
})();
