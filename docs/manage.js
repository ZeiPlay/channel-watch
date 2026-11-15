/* Добавление и удаление каналов прямо на сайте.
   Изменения записываются в channels.json через GitHub API, после чего запускается проверка.
   Токен хранится только в localStorage этого браузера. */
(function () {
  var LS = "channelwatch.cfg";
  var API = "https://api.github.com";
  var branch = null;

  function cfg() { try { return JSON.parse(localStorage.getItem(LS)) || {}; } catch (e) { return {}; } }
  function saveCfg(c) { try { localStorage.setItem(LS, JSON.stringify(c)); } catch (e) {} }
  function ready() { var c = cfg(); return !!(c.token && c.repo); }

  function guessRepo() {
    var m = location.hostname.match(/^([^.]+)\.github\.io$/);
    var seg = location.pathname.split("/").filter(Boolean)[0];
    return m && seg && seg.indexOf(".") < 0 ? m[1] + "/" + seg : "";
  }

  function h(tag, attrs, kids) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") e.textContent = attrs[k];
      else if (k === "class") e.className = attrs[k];
      else if (k.slice(0, 2) === "on") e[k] = attrs[k];
      else e.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { e.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return e;
  }

  // ---------- Ключ канала (то же правило, что chan_key в check.py) ----------
  function key(ch) {
    var ref = ch.handle || ch.channel_id || ch.source_url || ch.name || "";
    return (ch.platform + ":" + String(ref).trim().replace(/^@/, "")).toLowerCase();
  }

  // ---------- Разбор ссылок ----------
  function parseLink(raw) {
    var s = raw.trim();
    var short = s.match(/^(?:tg|тг|telegram):@?([A-Za-z][A-Za-z0-9_]{3,31})$/i);
    if (short) return { ch: { platform: "telegram", handle: "@" + short[1] } };
    if (/^@/.test(s)) {
      var at = s.match(/^@([A-Za-z][A-Za-z0-9_]{3,31})$/);
      if (at) return { ch: { platform: "telegram", handle: "@" + at[1] } };
      return { error: "неверное имя Telegram-канала" };
    }
    var url;
    try { url = new URL(/^https?:\/\//i.test(s) ? s : "https://" + s); } catch (e) { return { error: "не похоже на ссылку" }; }
    var host = url.hostname.toLowerCase().replace(/^(www|m)\./, "");
    var parts = url.pathname.split("/").filter(Boolean).map(function (p) { try { return decodeURIComponent(p); } catch (e) { return p; } });
    var p0 = parts[0] || "";

    if (host === "youtube.com" || host === "youtu.be") {
      if (host === "youtu.be" || ["watch", "shorts", "live", "embed"].indexOf(p0) >= 0)
        return { error: "это ссылка на видео, нужна ссылка на канал" };
      if (p0.charAt(0) === "@") return { ch: { platform: "youtube", handle: p0 } };
      if (p0 === "channel" && /^UC[\w-]{22}$/.test(parts[1] || "")) return { ch: { platform: "youtube", channel_id: parts[1] } };
      if ((p0 === "c" || p0 === "user") && parts[1])
        return { ch: { platform: "youtube", source_url: "https://www.youtube.com/" + p0 + "/" + encodeURIComponent(parts[1]) } };
      return { error: "не удалось найти канал в ссылке" };
    }
    if (host === "t.me" || host === "telegram.me" || host === "telegram.dog") {
      var name = p0 === "s" ? parts[1] : p0;
      if (!name || name.charAt(0) === "+" || ["joinchat", "c", "addlist", "share", "iv", "proxy", "socks"].indexOf(name) >= 0)
        return { error: "это закрытая группа или приглашение, нужен публичный канал t.me/имя" };
      if (!/^[A-Za-z][A-Za-z0-9_]{3,31}$/.test(name)) return { error: "неверное имя Telegram-канала" };
      return { ch: { platform: "telegram", handle: "@" + name } };
    }
    if (host === "tiktok.com") {
      if (p0.charAt(0) === "@" && p0.length > 1) return { ch: { platform: "tiktok", handle: p0 } };
      return { error: "нужна ссылка вида tiktok.com/@имя" };
    }
    if (host === "instagram.com") {
      if (!p0 || ["p", "reel", "reels", "stories", "explore", "tv", "accounts"].indexOf(p0) >= 0)
        return { error: "нужна ссылка на профиль, а не на публикацию" };
      return { ch: { platform: "instagram", handle: p0 } };
    }
    return { error: "платформа не поддерживается" };
  }

  // ---------- Разбор введённого текста: ссылка + необязательное название ----------
  // По одному каналу в строке: «@durov Дуров» или «Дуров @durov». Можно и несколько ссылок через пробел.
  var LINKISH = /^(https?:\/\/|@|(?:tg|тг|telegram):|[\w-]+(?:\.[\w-]+)+\/)/i;
  function cleanName(s) {
    return s.replace(/^[\s\-–—:|=«"'“(\[]+|[\s«"'»”)\]]+$/g, "").replace(/\s+/g, " ").slice(0, 60);
  }
  function parseInput(text) {
    var out = [];
    String(text).split(/\r?\n/).forEach(function (line) {
      var entries = [], prefix = [], cur = null;
      line.split(/\s+/).filter(Boolean).forEach(function (tok) {
        var t = tok.replace(/,+$/, "");
        if (!t) return;
        if (LINKISH.test(t)) { cur = { raw: t, words: [] }; entries.push(cur); }
        else if (cur) cur.words.push(t);
        else prefix.push(t);
      });
      if (entries.length && prefix.length && !entries[0].words.length) entries[0].words = prefix;
      if (!entries.length && prefix.length) entries.push({ raw: prefix.join(" "), words: [] });
      entries.forEach(function (e) { out.push({ raw: e.raw, name: cleanName(e.words.join(" ")) }); });
    });
    return out;
  }

  // ---------- GitHub API ----------
  function b64dec(s) {
    var bin = atob(s.replace(/\s/g, ""));
    var bytes = Uint8Array.from(bin, function (c) { return c.charCodeAt(0); });
    return new TextDecoder().decode(bytes);
  }
  function b64enc(str) {
    var bin = "";
    new TextEncoder().encode(str).forEach(function (b) { bin += String.fromCharCode(b); });
    return btoa(bin);
  }
  function gh(path, opt) {
    var c = cfg();
    opt = opt || {};
    var headers = {
      "Authorization": "Bearer " + c.token,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28"
    };
    if (opt.body) headers["Content-Type"] = "application/json";
    return fetch(API + "/repos/" + c.repo + path, { method: opt.method || "GET", headers: headers, body: opt.body });
  }
  function explain(res) {
    if (res.status === 401) return "токен не подходит или истёк";
    if (res.status === 403 || res.status === 404) return "нет доступа: проверьте название репозитория и права токена (Contents: Read and write)";
    return "ошибка GitHub " + res.status;
  }

  // Читает channels.json, применяет mutate(json) и сохраняет. mutate возвращает false, если менять нечего.
  function modify(mutate, message) {
    function attempt(n) {
      return gh("/contents/channels.json").then(function (res) {
        if (!res.ok) throw new Error(explain(res));
        return res.json();
      }).then(function (file) {
        var json = JSON.parse(b64dec(file.content));
        if (mutate(json) === false) return false;
        var body = JSON.stringify({ message: message, content: b64enc(JSON.stringify(json, null, 2) + "\n"), sha: file.sha });
        return gh("/contents/channels.json", { method: "PUT", body: body }).then(function (res) {
          if ((res.status === 409 || res.status === 422) && n < 3) return attempt(n + 1);
          if (!res.ok) throw new Error(explain(res));
          return true;
        });
      });
    }
    return attempt(1);
  }

  function dispatch() {
    var go = function (ref) {
      return gh("/actions/workflows/check.yml/dispatches", { method: "POST", body: JSON.stringify({ ref: ref }) })
        .then(function (r) { return r.ok; });
    };
    if (branch) return go(branch).catch(function () { return false; });
    return gh("").then(function (r) { return r.ok ? r.json() : { default_branch: "main" }; })
      .then(function (repo) { branch = repo.default_branch || "main"; return go(branch); })
      .catch(function () { return false; });
  }

  // Опрашивает репозиторий, пока проверка не запишет новые данные (обычно около минуты)
  function watchForUpdate(onDone) {
    var app = window.ChannelApp;
    if (!app) return;
    var since = Date.now() - 5000, tries = 0, busy = false;
    var timer = setInterval(function () {
      if (busy) return;
      tries++; busy = true;
      app.refresh().then(function (g) {
        busy = false;
        if (g && g.getTime() > since) { clearInterval(timer); if (onDone) onDone(); }
        else if (tries >= 36) clearInterval(timer);
      });
    }, 10000);
  }

  // ---------- Интерфейс ----------
  var panel = h("section", { class: "panel", hidden: "" });
  var addBtn = h("button", { class: "addbtn", type: "button", text: "＋ Добавить каналы" });
  var msgBox = null;

  function setMsg(x, cls) {
    if (!msgBox) return;
    msgBox.className = "msg";
    msgBox.textContent = "";
    (Array.isArray(x) ? x : [{ t: x, c: cls }]).forEach(function (l) {
      msgBox.appendChild(h("div", { class: l.c || "", text: l.t }));
    });
  }

  function renderSetup() {
    var c = cfg();
    var repo = h("input", { type: "text", placeholder: "ваш-логин/channel-watch", value: c.repo || guessRepo(), autocapitalize: "off", autocorrect: "off", spellcheck: "false" });
    var token = h("input", { type: "password", placeholder: "github_pat_...", autocomplete: "off" });
    msgBox = h("div", { class: "msg" });
    var save = h("button", { class: "primary", type: "button", text: "Сохранить", onclick: function () {
      var r = repo.value.trim().replace(/^https?:\/\/github\.com\//, "").replace(/\/+$/, "");
      var t = token.value.trim();
      if (!/^[\w.-]+\/[\w.-]+$/.test(r)) return setMsg("Репозиторий в формате логин/название", "err");
      if (!t) return setMsg("Вставьте токен", "err");
      saveCfg({ repo: r, token: t });
      document.body.classList.add("can-edit");
      renderAdd();
    } });
    var steps = h("ol", {}, [
      h("li", { text: "Откройте github.com/settings/personal-access-tokens/new" }),
      h("li", { text: "Название: channel-watch. Срок: например, 1 год." }),
      h("li", { text: "Repository access → Only select repositories → выберите свой репозиторий." }),
      h("li", { text: "Permissions → Repository permissions: Contents = Read and write, Actions = Read and write." }),
      h("li", { text: "Нажмите Generate token и скопируйте его сюда." })
    ]);
    panel.textContent = "";
    [h("h2", { text: "Подключение к GitHub (один раз)" }),
     h("p", { text: "Токен хранится только в этом браузере и нужен, чтобы сайт мог дописывать каналы в ваш список." }),
     h("details", {}, [h("summary", { text: "Как получить токен" }), steps]),
     h("label", { text: "Репозиторий" }), repo,
     h("label", { text: "Токен" }), token,
     h("div", { class: "row" }, [save]), msgBox
    ].forEach(function (n) { panel.appendChild(n); });
  }

  function renderAdd() {
    var ta = h("textarea", { placeholder: "Один канал на строку, название по желанию:\n@durov Дуров\nyoutube.com/@mrbeast MrBeast\ntiktok.com/@имя\ninstagram.com/имя", autocapitalize: "off", spellcheck: "false" });
    msgBox = h("div", { class: "msg" });
    var go = h("button", { class: "primary", type: "button", text: "Добавить" });
    go.onclick = function () {
      var items = parseInput(ta.value);
      if (!items.length) return setMsg("Вставьте хотя бы одну ссылку", "err");
      var good = [], bad = [];
      items.forEach(function (it) {
        var r = parseLink(it.raw);
        if (r.ch) {
          if (it.name) r.ch.name = it.name;
          good.push(r.ch);
        } else bad.push(it.raw + " — " + r.error);
      });
      if (!good.length) return setMsg("Ничего не добавлено:\n" + bad.join("\n"), "err");
      go.disabled = true; setMsg("Сохраняю…", "");
      var added = [], dups = [], renamed = [];
      modify(function (json) {
        added = []; dups = []; renamed = [];
        var have = {};
        json.channels.forEach(function (c) { have[key(c)] = c; });
        good.forEach(function (ch) {
          var k = key(ch);
          if (have[k]) {
            if (ch.name && have[k].name !== ch.name) { have[k].name = ch.name; renamed.push(ch.name); }
            else dups.push(ch.handle || ch.channel_id || ch.source_url);
            return;
          }
          have[k] = ch; json.channels.push(ch); added.push(ch);
        });
        return added.length + renamed.length > 0;
      }, "Добавлены каналы через сайт").then(function (changed) {
        if (!changed) return [{ t: "Эти каналы уже в списке." }];
        return dispatch().then(function (ok) {
          ta.value = "";
          if (ok) watchForUpdate(function () {
            if (msgBox) msgBox.appendChild(h("div", { class: "ok", text: "Готово, данные на сайте обновились." }));
          });
          return [
            { t: (added.length ? "Добавлено: " + added.length + "." : "Названия обновлены: " + renamed.length + "."), c: "ok" },
            ok ? { t: "Проверка запущена, данные появятся примерно через минуту, страница обновится сама.", c: "ok" }
               : { t: "Сохранено, но запустить проверку сразу не вышло (проверьте право Actions у токена). Данные обновятся по расписанию.", c: "err" }
          ];
        });
      }).then(function (lines) {
        if (added.length && renamed.length) lines.push({ t: "Названия обновлены: " + renamed.length + "." });
        if (dups.length) lines.push({ t: "Уже были в списке: " + dups.length + "." });
        if (bad.length) lines.push({ t: "Не распознано:\n" + bad.join("\n"), c: "err" });
        setMsg(lines);
      }).catch(function (e) { setMsg("Не получилось: " + e.message, "err"); })
        .then(function () { go.disabled = false; });
    };
    var settings = h("button", { class: "link", type: "button", text: "Настройки", onclick: renderSetup });
    var logout = h("button", { class: "link", type: "button", text: "Выйти", onclick: function () {
      saveCfg({ repo: cfg().repo }); document.body.classList.remove("can-edit"); renderSetup();
    } });
    panel.textContent = "";
    [h("h2", { text: "Добавить каналы" }),
     h("p", { text: "YouTube, TikTok и Instagram по ссылке, Telegram как @имя. После ссылки можно написать своё название, оно будет на карточке. Можно вставить сразу много." }),
     ta, h("div", { class: "row" }, [go, settings, logout]), msgBox
    ].forEach(function (n) { panel.appendChild(n); });
  }

  function remove(c, cardEl) {
    if (!ready()) return;
    if (!window.confirm("Удалить канал «" + c.name + "» из списка?")) return;
    var k = c.key || key(c);
    var removed = false;
    modify(function (json) {
      var before = json.channels.length;
      json.channels = json.channels.filter(function (x) { return key(x) !== k; });
      removed = json.channels.length < before;
      return removed;
    }, "Удалён канал: " + c.name).then(function (changed) {
      if (changed && cardEl && cardEl.parentNode) cardEl.parentNode.removeChild(cardEl);
      return changed ? dispatch().then(function (ok) { if (ok) watchForUpdate(); }) : null;
    }).then(function () {
      if (!removed) window.alert("Канала нет в списке (возможно, он уже удалён).");
    }).catch(function (e) { window.alert("Не получилось удалить: " + e.message); });
  }

  function init() {
    var header = document.querySelector("header");
    if (!header) return;
    header.appendChild(addBtn);
    header.parentNode.insertBefore(panel, header.nextSibling);
    if (ready()) document.body.classList.add("can-edit");
    addBtn.onclick = function () {
      if (panel.hidden) { panel.hidden = false; (ready() ? renderAdd : renderSetup)(); }
      else panel.hidden = true;
    };
  }

  window.ChannelManage = { remove: remove, key: key, parseLink: parseLink, parseInput: parseInput };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
