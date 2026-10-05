/* Админ-режим витрины (?admin=1) — закрытый модуль IPAK.
   Лежит в site/admin/ рядом с закрытыми данными и НЕ публикуется никогда (deploy.py: FORBIDDEN "admin").
   index.html подключает этот файл тегом <script> только при ?admin=1 (локально, в том числе по file://)
   и вызывает window.IPAK_ADMIN(api): api — функции и состояние витрины, ответ — крючки, которые витрина зовёт:
     loadExtra()        закупочные данные: products-admin.js целиком или site/admin/meta.js + корзины <xx>.js по id
     apply(p)           новая часть каталога: дописать закупку товару
     all()              весь каталог пришёл: догрузить все корзины (сводка по источникам, поиск по артикулу)
     header()           HTML для шапки каталога: курсы, сводки по источникам и брендам, ссылка на разбор заказа
     textHit(p, raw)    поиск ещё и по источнику, артикулу, названию там
     card(p)            блок «откуда товар, закупка, себестоимость, маржа» в карточке и в окне товара
     forCards(list)     догрузить закупку показанных карточек и переписать их блоки
     forProduct(p)      то же для открытого окна товара (#pvAdm)
     renderLookup()     страница #/admin/lookup — разбор заказа из Telegram (как order_lookup.py)
     lookupInput()      ввод в поле разбора
     refreshLookup()    пришла новая часть каталога — пересчитать разбор
   ВАЖНО: catalog_files.write_admin() при каждой сборке удаляет из site/admin/ всё, кроме своих корзин и meta.js, —
   этот файл должен быть в его списке keep (иначе python run.py его сотрёт). */
window.IPAK_ADMIN = function (api) {
  "use strict";
  var esc = api.esc, fmt = api.fmt, safeUrl = api.safeUrl, NBSP = api.NBSP;

  /* ---------- закупочные данные ---------- */
  // Небольшой каталог — products-admin.js целиком (window.DEALS_ADMIN = {"<id>": {source, url, price_now, ...}}),
  // большой — site/admin/<корзина>.js по id: карточка товара, разбор заказа и показанные карточки догружают свои
  // корзины, сводка — когда загрузится всё.
  var ADMIN_KEYS = ["source", "source_item_id", "url", "title_original", "category_original", "price_now", "price_old", "currency", "cost_uzs", "margin_uzs"];
  var ADM = { mode: "", meta: null, items: {}, loaded: {}, loading: {}, complete: false };
  var missing = false;                 // закрытых файлов нет совсем

  function admRecord(p) {
    if (ADM.mode === "all") {
      var extra = window.DEALS_ADMIN && typeof window.DEALS_ADMIN === "object" ? window.DEALS_ADMIN : {};
      return extra[p._code] || (p.source != null ? extra[p.source + ":" + p.source_item_id] : null);
    }
    return ADM.items[p._code] || null;
  }
  function admApply(p, early) {
    var adm = {};
    ADMIN_KEYS.forEach(function (k) { if (p[k] != null) adm[k] = p[k]; });   // старый формат: всё было в products.js
    var x = admRecord(p);
    if (x && typeof x === "object") Object.assign(adm, x);
    if (!Object.keys(adm).length) return;
    p._adm = adm;
    p._search = null;
    // скидка по закупочным ценам — только до сортировок (при запуске), иначе порядок выдачи разъедется
    if (early && !p._disc && adm.price_old > adm.price_now && adm.price_now > 0) p._disc = Math.round((1 - adm.price_now / adm.price_old) * 1000) / 10;
  }
  function loadExtra() {
    var all = function () { ADM.mode = "all"; ADM.complete = true; api.products().forEach(function (p) { admApply(p, true); }); };
    if (window.DEALS_ADMIN) { all(); return Promise.resolve(); }
    return api.loadScript("products-admin.js?t=" + Date.now()).then(all, function () {
      return api.getData("admin/meta.js", "a/meta", true).then(function (meta) {
        ADM.mode = "shards";
        ADM.meta = meta || {};
        window.DEALS_ADMIN = { _meta: ADM.meta._meta || {} };
      }, function () { missing = true; all(); });
    });
  }
  function admBucket(id) { return api.hash32(String(id)) & ((ADM.meta && ADM.meta.shards || 16) - 1); }
  function admLoaded(id) { return ADM.mode !== "shards" || !!ADM.loaded[admBucket(id)]; }
  function loadAdmBucket(b) {
    if (ADM.loading[b]) return ADM.loading[b];
    var key = b.toString(16), w = ADM.meta.prefix_len || 1;
    while (key.length < w) key = "0" + key;
    return (ADM.loading[b] = api.getData("admin/" + key + ".js", "a/" + key, true).then(function (obj) {
      var it = (obj && obj.items) || {}, byId = api.byId();
      Object.keys(it).forEach(function (id) { ADM.items[id] = it[id]; var p = byId.get(id); if (p) admApply(p); });
      ADM.loaded[b] = true;
    }, function () { ADM.loaded[b] = true; }));
  }
  function ensureAdmin(ids) {
    if (ADM.mode !== "shards") return Promise.resolve();
    var bs = {};
    ids.forEach(function (id) { bs[admBucket(String(id).toUpperCase())] = 1; });
    return Promise.all(Object.keys(bs).map(function (b) { return loadAdmBucket(+b); }));
  }
  // только что показанные карточки выдачи: догрузить их закупку и переписать блок .adm
  function forCards(list) {
    if (ADM.mode !== "shards") return;
    var need = list.filter(function (p) { return !p._adm && !admLoaded(p._code); });
    if (!need.length) return;
    ensureAdmin(need.map(function (p) { return p._code; })).then(function () {
      need.forEach(function (p) {
        var el = api.els.grid.querySelector('.adm[data-adm="' + p._code + '"]');
        if (el) el.outerHTML = adminHtml(p);
      });
    });
  }
  function forProduct(p) {
    ensureAdmin([p._code]).then(function () {
      var box = api.$("pvAdm");
      if (api.pvCur() === p && box) box.innerHTML = adminHtml(p);
    });
  }
  // весь каталог загружен — закупка всех корзин в фоне (сводка по источникам, поиск по артикулу)
  function all() {
    if (ADM.mode !== "shards" || ADM.complete) return;
    var n = ADM.meta.shards || 16, b = 0, left = n;
    var next = function () {
      if (b >= n) return;
      loadAdmBucket(b++).then(function () {
        if (--left) { api.idle(next); return; }
        ADM.complete = true;
        api.renderHeader();
        if (api.state().q && api.view() === "catalog") api.refreshCatalog();
      });
    };
    for (var k = 0; k < 6; k++) next();      // файлы локальные — по 6 сразу
  }

  /* ---------- шапка каталога: курсы, сводки ---------- */
  function fmtMoney(v, cur) {
    v = Number(v);
    if (!isFinite(v)) return "";
    var whole = Math.abs(v - Math.round(v)) < 0.005;
    var s = whole ? fmt(v) : (function () {
      var parts = Math.abs(v).toFixed(2).split(".");
      return (v < 0 ? "-" : "") + fmt(parts[0]) + "," + parts[1];
    })();
    return s + NBSP + (cur || "");          // код валюты — как в закрытых данных
  }
  function fmtRate(v) {
    var parts = Number(v).toFixed(2).split(".");
    return fmt(parts[0]) + "," + parts[1];
  }
  function header() {
    var meta = window.DEALS_ADMIN && window.DEALS_ADMIN._meta, S = api.summary() || {}, products = api.products();
    var rates = S.rates || (meta && meta.rates) || {};
    var rateStr = Object.keys(rates).map(function (k) { return "1 " + esc(k) + " = " + fmtRate(rates[k]) + NBSP + "сум"; }).join(" · ");
    var withAdm = products.filter(function (p) { return p._adm; }).length;
    var html = '<p class="admin-line"><b>Админ-режим.</b> ' + (rateStr ? "Курсы ЦБ: " + rateStr + ". " : "") +
      (missing && !withAdm ? "Закрытых данных нет (ни products-admin.js, ни site/admin/) — закупка и маржа недоступны."
        : ADM.mode === "shards" && !ADM.complete ? "Закупка подгружается по частям (site/admin/): в карточке и в разборе заказа — сразу, сводка — когда загрузится весь каталог."
        : "Закупка известна для " + fmt(withAdm) + " из " + fmt(products.length) + ". Покупатели закрытые файлы (products-admin.js, site/admin/) не загружают — не выкладывайте их на хостинг.") + "</p>";
    html += '<p class="admin-line"><a href="#/admin/lookup">Разобрать заказ из Telegram (коды → закупка) →</a></p>';
    html += statsTable("Сводка по источникам", "Источник", S.by_source || sourceStats());
    html += statsTable("Сводка по брендам", "Бренд", S.by_brand || (meta && meta.by_brand));   // большой каталог: в закрытых файлах
    return html;
  }
  // сводка по источникам считается на месте из закупочных данных (в публичном summary её нет)
  function sourceStats() {
    var out = {};
    api.products().forEach(function (p) {
      var k = p._adm && p._adm.source;
      if (!k) return;
      var s = out[k] || (out[k] = { count: 0, price_min: Infinity, price_max: 0, with_discount: 0, discount_max: 0, _sum: 0 });
      s.count++;
      s.price_min = Math.min(s.price_min, p._price);
      s.price_max = Math.max(s.price_max, p._price);
      if (p._disc > 0) { s.with_discount++; s._sum += p._disc; s.discount_max = Math.max(s.discount_max, p._disc); }
    });
    Object.keys(out).forEach(function (k) { var s = out[k]; s.discount_avg = s.with_discount ? s._sum / s.with_discount : 0; });
    return out;
  }
  // таблица из summary.by_source / by_brand: {ключ: {count, price_min, price_max, with_discount, discount_max, discount_avg}}
  function statsTable(title, col, obj, label) {
    obj = obj || {};
    var keys = Object.keys(obj).filter(function (k) { return obj[k]; });
    if (!keys.length) return "";
    keys.sort(function (a, b) { return obj[b].count - obj[a].count || a.localeCompare(b, "ru"); });
    return '<details class="admin-brands"><summary>' + esc(title) + " (" + keys.length + ")</summary><table><thead><tr>" +
      "<th>" + esc(col) + "</th><th>Шт.</th><th>Без скидки</th><th>Цена, сум</th><th>Скидка макс.</th><th>Скидка ср.</th></tr></thead><tbody>" +
      keys.map(function (k) {
        var s = obj[k];
        var noDisc = s.with_discount != null ? s.count - s.with_discount : 0;
        return "<tr><td>" + esc(label ? label(k) : k) + "</td><td>" + fmt(s.count) + "</td><td>" + (noDisc ? fmt(noDisc) : "—") +
          "</td><td>" + fmt(s.price_min) + " – " + fmt(s.price_max) +
          "</td><td>" + api.discShown(s.discount_max) + "%</td><td>" + api.discShown(s.discount_avg) + "%</td></tr>";
      }).join("") + "</tbody></table></details>";
  }

  /* ---------- поиск по закрытым полям (источник, артикул, название там) ---------- */
  function admText(p) {
    if (p._search != null) return p._search;
    var a = p._adm || {};
    return (p._search = " " + api.normQ([a.source, a.source_item_id, a.title_original].join(" ")) + " ");
  }
  function textHit(p, raw) { return !!p._adm && admText(p).indexOf(raw) >= 0; }

  /* ---------- карточка и окно товара: откуда товар, закупка, себестоимость, маржа ---------- */
  function adminHtml(p) {
    var a = p._adm || {}, rows = [];
    var url = safeUrl(a.url || "", false);
    var src = a.source || "—";
    rows.push(["Источник", url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(src) + " ↗</a>" : esc(src)]);
    if (a.source_item_id != null) rows.push(["Артикул", esc(a.source_item_id)]);
    if (a.title_original) rows.push(["Название", esc(a.title_original)]);
    if (a.category_original) rows.push(["Категория", esc(a.category_original)]);
    var now = Number(a.price_now), old = Number(a.price_old);
    if (now > 0) rows.push(["Цена там", (old > now ? "<s>" + fmtMoney(old, a.currency) + "</s> → " : "") + "<b>" + fmtMoney(now, a.currency) + "</b>"]);
    if (a.cost_uzs != null) rows.push(["Себестоимость", fmt(a.cost_uzs) + NBSP + "сум"]);
    if (a.margin_uzs != null) {
      var pct = a.cost_uzs ? " (" + Math.round(a.margin_uzs / a.cost_uzs * 100) + "%)" : "";
      rows.push(["Маржа", fmt(a.margin_uzs) + NBSP + "сум" + pct]);
    }
    rows.push(["Скидка", p._disc > 0 ? p._disc.toFixed(1).replace(".", ",") + "%" : "нет"]);
    if (p.fetched_at) rows.push(["Собрано", esc(tsText(p.fetched_at))]);
    if (p.first_seen) rows.push(["На сайте с", esc(tsText(p.first_seen))]);
    if (a.sold_out_since) rows.push(["Нет в наличии с", esc(tsText(a.sold_out_since))]);
    return '<dl class="adm" data-adm="' + esc(p._code) + '">' + rows.map(function (r) { return "<div><dt>" + r[0] + "</dt><dd>" + r[1] + "</dd></div>"; }).join("") + "</dl>";
  }
  // время: строка ISO или миллисекунды (части каталога) -> «2026-10-03 13:12»
  function tsText(v) {
    if (typeof v === "number") { try { v = new Date(v).toISOString(); } catch (e) { return ""; } }
    return String(v).replace("T", " ").slice(0, 16);
  }

  /* ---------- разбор заказа из Telegram (#/admin/lookup) — как order_lookup.py ---------- */
  // название магазина — только из закрытых данных: домен ссылки или код источника
  function shopName(adm) {
    try { return new URL(adm.url).hostname.replace(/^www\./, ""); } catch (e) { return adm.source || "?"; }
  }
  function extractCodes(text) {
    var re = /(^|[^A-Za-z0-9])([A-Za-z0-9]{7})(?=[^A-Za-z0-9]|$)/g, m, found = [], unknown = [], seen = new Set();
    while ((m = re.exec(text || ""))) {
      var code = m[2].toUpperCase();
      re.lastIndex = m.index + m[0].length;
      if (seen.has(code)) continue;
      seen.add(code);
      if (api.findProduct(code)) found.push(code);
      else if (/\d/.test(code) && /[A-Z]/.test(code)) unknown.push(code);
    }
    return { found: found, unknown: unknown };
  }
  function srcPrice(v) { var f = Number(v); return isFinite(f) ? (f === Math.round(f) ? String(f) : f.toFixed(2)) : "?"; }
  function lookupHtml(text) {
    if (!api.str(text)) return "";
    if (!api.ready()) return "<p>Каталог ещё загружается…</p>";
    var r = extractCodes(text), total = 0;
    var notYet = api.complete() ? "" : " (каталог ещё загружается — проверю ещё раз, когда загрузится весь)";
    if (!r.found.length) return "<p>Кодов товаров в тексте не нашёл." + (r.unknown.length ? " Похоже на код, но нет в каталоге: " + esc(r.unknown.join(", ")) + notYet : "") + "</p>";
    // закупка по частям (site/admin/): догрузить корзины этих кодов и перерисовать
    var need = r.found.filter(function (c) { var p = api.findProduct(c); return p && !p._adm && !admLoaded(p._code); });
    if (need.length) ensureAdmin(need).then(refreshLookup);
    var html = '<ul class="lk-list">' + r.found.map(function (code) {
      var p = api.findProduct(code), a = p._adm || {}, img = api.goodImgs(p)[0], rows = [];
      total += p._price;
      rows.push(["Наша цена", fmt(p._price) + " сум" + (p._disc ? " (скидка " + api.discShown(p._disc) + "%)" : "")]);
      rows.push(["Размеры", (p._sizes.join(", ") || "—") + (p._out ? " — НЕТ В НАЛИЧИИ" : "") + (p._sizesOut.length ? "; закончились: " + p._sizesOut.join(", ") : "")]);
      if (a.source || a.url) {
        var url = safeUrl(a.url || "", false);
        rows.push(["Магазин", esc(shopName(a)) + (url ? ' · <a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">открыть ↗</a>' : "")]);
        if (a.price_now != null) rows.push(["Цена там", esc(srcPrice(a.price_now) + " " + (a.currency || "")) + (a.price_old && a.price_old !== a.price_now ? " (было " + esc(srcPrice(a.price_old)) + ")" : "")]);
        if (a.cost_uzs != null) rows.push(["Себест. / маржа", fmt(a.cost_uzs) + " / " + (a.margin_uzs != null ? fmt(a.margin_uzs) : "?") + " сум"]);
        if (a.title_original) rows.push(["Название там", esc(a.title_original)]);
      } else rows.push(["Закупка", admLoaded(p._code) ? "нет данных (в закрытых файлах нет этого кода)" : "загружаем…"]);
      return '<li class="lk-item"><div class="media' + (img ? "" : " noimg") + '" data-mono="' + esc(api.monogram(p._brand)) + '">' +
        (img ? '<img src="' + esc(img) + '" alt="" loading="lazy" referrerpolicy="no-referrer">' : "") + "</div>" +
        "<div><h3>[" + esc(code) + "] " + esc(p._brand) + " — " + esc(p._title) + "</h3><dl>" +
        rows.map(function (x) { return "<dt>" + x[0] + "</dt><dd>" + (x[0] === "Наша цена" || x[0] === "Размеры" || x[0] === "Себест. / маржа" ? esc(x[1]) : x[1]) + "</dd>"; }).join("") +
        '</dl><p style="margin:8px 0 0"><a href="#/admin/lookup?p=' + encodeURIComponent(code) + '" data-open="' + esc(code) + '">Открыть карточку</a></p></div></li>';
    }).join("") + "</ul>";
    html += '<p class="lk-sum">Кодов: ' + r.found.length + (r.found.length > 1 ? ", сумма по нашим ценам (по 1 шт.): " + fmt(total) + " сум" : "") + "</p>";
    if (r.unknown.length) html += '<p class="lk-sum">Похоже на код, но нет в каталоге (снят с сайта?): ' + esc(r.unknown.join(", ")) + notYet + "</p>";
    return html;
  }
  var lookupText = "";
  function renderLookup() {
    api.els.viewPage.innerHTML = '<div class="wrap page">' + api.crumbs("Разбор заказа") +
      '<div class="lookup" style="max-width:900px"><div class="prose"><h1>Разбор заказа</h1>' +
      "<p>Вставьте сообщение покупателя: найдём коды товаров (7 знаков, регистр не важен) и покажем закупочные данные. Страница видна только в режиме ?admin=1.</p></div>" +
      '<label class="sr" for="lkText">Текст заказа</label><textarea id="lkText" placeholder="Здравствуйте, хочу XZXUTRY размер 2XL и 2fujjww">' + esc(lookupText) + "</textarea>" +
      '<div id="lkOut" aria-live="polite">' + lookupHtml(lookupText) + "</div></div></div>";
  }
  function refreshLookup() {
    if (api.view() !== "admin/lookup" || !lookupText) return;
    var out = api.$("lkOut");
    if (out) out.innerHTML = lookupHtml(lookupText);
  }
  var lookupInput = api.debounce(function () {
    var t = api.$("lkText"), out = api.$("lkOut");
    if (!t || !out) return;
    lookupText = t.value.slice(0, 20000);
    out.innerHTML = lookupHtml(lookupText);
  }, 200);

  return {
    loadExtra: loadExtra,
    apply: function (p) { if (ADM.mode) admApply(p); },
    all: all,
    header: header,
    textHit: textHit,
    card: adminHtml,
    forCards: forCards,
    forProduct: forProduct,
    renderLookup: renderLookup,
    lookupInput: lookupInput,
    refreshLookup: refreshLookup
  };
};
