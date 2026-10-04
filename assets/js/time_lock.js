(function () {
  "use strict";

  var LONG_SECONDS = parseInt(document.body.dataset.longSeconds, 10);
  var defaultSeconds = parseInt(document.body.dataset.defaultSeconds, 10);
  var TEST_MODE = document.body.dataset.testMode === "true";
  var TOKEN = document.querySelector('meta[name="timelock-token"]').content;
  var DEBUG = document.body.dataset.debug === "true"; // follows the server DEBUG_MODE

  var serverOffset = 0;      // server time minus browser time, seconds
  var pendingRevealId = null;
  var pendingExtend = null;  // { id, label, seconds }
  var selected = null;       // Date chosen in the calendar (local time)
  var viewYear, viewMonth;
  var settings = { sound_enabled: true, sound_volume: 70 };
  var maxRandomDays = 30;
  var randomMode = false;    // the unlock moment will be chosen by the server, at random
  var choice = { type: "default" };   // how the unlock time was picked, remembered for next time
  var PREF_KEY = "time-lock-prefs";
  var LONG_TEXT = "You are locking this for more than 30 days. Nobody can open it sooner, not even you. Are you sure?";
  var PLACE_ICONS = { "This folder": "bi-bullseye", "Home": "bi-house-door-fill", "Pictures": "bi-images",
    "Downloads": "bi-download", "Desktop": "bi-display", "Documents": "bi-file-earmark-text",
    "/media": "bi-usb-drive", "/mnt": "bi-hdd" };
  var knownLocked = null;    // ids seen locked on the previous refresh (null = first refresh)
  var dingWaiting = false;
  var lastSignature = "";

  function $(id) { return document.getElementById(id); }
  function log() { if (DEBUG) console.log.apply(console, ["[time-lock]"].concat([].slice.call(arguments))); }
  function nowSec() { return Math.floor(Date.now() / 1000) + serverOffset; }
  function modal(id) { return bootstrap.Modal.getOrCreateInstance($(id)); }

  function fmt(epoch) {
    return new Date(epoch * 1000).toLocaleString(undefined,
      { weekday: "short", day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function remaining(seconds) {
    if (seconds <= 0) return "now";
    var d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600),
        m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
    if (d) return d + "d " + h + "h";
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + s + "s";
    return s + "s";
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function tip(node, text) {
    node.setAttribute("title", text);
    node.setAttribute("data-bs-toggle", "tooltip");
    return node;
  }

  function initTooltips(root) {
    root.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (n) { bootstrap.Tooltip.getOrCreateInstance(n); });
  }

  function disposeTooltips(root) {
    // Let any tooltip finish fading before it is cleared away: disposing mid-fade makes
    // Bootstrap throw "Cannot convert undefined or null to object".
    root.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (n) {
      var t = bootstrap.Tooltip.getInstance(n);
      if (!t) return;
      t.hide();
      setTimeout(function () { try { t.dispose(); } catch (e) { /* already gone */ } }, 400);
    });
  }

  function alertBox(kind, text) {
    var box = el("div", "alert alert-" + kind + " alert-dismissible fade show", text);
    var close = el("button", "btn-close");
    close.setAttribute("data-bs-dismiss", "alert");
    box.appendChild(close);
    $("alerts").appendChild(box);
    setTimeout(function () { box.remove(); }, 9000);
  }

  async function api(url, options) {
    options = options || {};
    options.headers = Object.assign({ "X-TimeLock-Token": TOKEN }, options.headers || {});
    var res = await fetch(url, options);
    if (!res.ok) {
      var body = {};
      try { body = await res.json(); } catch (e) { /* not json */ }
      var err = new Error(body.error || "Something went wrong");
      err.body = body; err.status = res.status;
      throw err;
    }
    return res;
  }

  async function apiJson(url, options) { return (await api(url, options)).json(); }

  function postJson(url, data, method) {
    return apiJson(url, { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });
  }

  // ---------- remembered preferences (browser local storage) ----------
  function loadPrefs() {
    try { return JSON.parse(localStorage.getItem(PREF_KEY)) || {}; } catch (e) { return {}; }
  }
  function savePrefs(patch) {
    try { localStorage.setItem(PREF_KEY, JSON.stringify(Object.assign(loadPrefs(), patch))); } catch (e) { /* storage blocked */ }
  }
  function forgetPrefs() {
    try { localStorage.removeItem(PREF_KEY); } catch (e) { /* storage blocked */ }
  }

  // ---------- sound ----------
  var ding = $("dingSound");

  function applyVolume() { ding.volume = Math.max(0, Math.min(1, (settings.sound_volume || 0) / 100)); }

  function playDing(force) {
    if (!force && !settings.sound_enabled) return;
    applyVolume();
    ding.currentTime = 0;
    var p = ding.play();
    if (p && p.catch) p.catch(function () { dingWaiting = true; log("sound blocked until the next click"); });
  }

  document.addEventListener("click", function () {
    if (dingWaiting) { dingWaiting = false; playDing(); }
  });

  // ---------- banners ----------
  function banners(data) {
    var box = $("statusBanners");
    box.replaceChildren();
    function add(kind, icon, text) {
      var a = el("div", "alert alert-" + kind);
      a.appendChild(el("i", "bi " + icon + " me-2"));
      a.appendChild(document.createTextNode(text));
      box.appendChild(a);
    }
    if (data.paused_until) {
      add("danger", "bi-pause-circle-fill",
        "An older copy of the database was put back, so unlocking is paused until " + fmt(data.paused_until) + ".");
    }
    if (data.clock_suspect) add("warning", "bi-exclamation-triangle-fill", data.clock_suspect);
    if (data.settings_damaged) add("warning", "bi-gear", "Your settings could not be verified, so the safe defaults are being used.");
  }

  // ---------- lists ----------
  function row(item) {
    var tr = el("tr");
    tr.appendChild(el("td", "", item.label));
    var type = el("td");
    type.appendChild(el("i", "bi " + (item.kind === "image" ? "bi-image" : "bi-fonts") + " me-1"));
    type.appendChild(document.createTextNode(item.kind === "image" ? "Image" : "Text"));
    tr.appendChild(type);
    tr.appendChild(el("td", "", fmt(item.created_at)));

    var when = el("td");
    if (item.b_concealed) {
      when.appendChild(el("i", "bi bi-eye-slash me-1"));
      when.appendChild(document.createTextNode("Hidden"));
    } else {
      when.textContent = fmt(item.unlock_at) + "  (in " + remaining(item.unlock_at - nowSec()) + ")";
    }
    tr.appendChild(when);

    var actions = el("td", "text-end");
    var longer = tip(el("button", "btn btn-secondary btn-sm me-2"), "Add more time to this lock. Time can only be added.");
    longer.appendChild(el("i", "bi bi-plus-circle me-1"));
    longer.appendChild(document.createTextNode("Lock longer"));
    longer.addEventListener("click", function () {
      pendingExtend = { id: item.id, label: item.label, seconds: 86400 };
      $("extendName").textContent = item.label;
      resetExtendConfirm();
      modal("extendModal").show();
    });
    actions.appendChild(longer);
    if (item.b_concealed) {
      var btn = tip(el("button", "btn btn-secondary btn-sm"), "Reveal when this unlocks. It costs an extra 48 hours.");
      btn.appendChild(el("i", "bi bi-eye me-1"));
      btn.appendChild(document.createTextNode("Reveal Now"));
      btn.addEventListener("click", function () { pendingRevealId = item.id; modal("revealModal").show(); });
      actions.appendChild(btn);
    }
    tr.appendChild(actions);
    return tr;
  }

  async function downloadImage(item) {
    try {
      var res = await api("/api/items/" + item.id + "/image.jpg");
      var url = URL.createObjectURL(await res.blob());
      var a = el("a");
      a.href = url; a.download = item.exported_path || "image.jpg";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
    } catch (e) { alertBox("danger", e.message); }
  }

  function card(item) {
    var col = el("div", "col-md-6 col-xl-4");
    var c = el("div", "card card-custom h-100");
    var body = el("div", "card-body");
    body.appendChild(el("h5", "card-title", item.label));
    body.appendChild(el("p", "text-muted small", "Unlocked " + fmt(item.unlock_at)));
    if (item.state === "tampered") {
      body.appendChild(el("div", "alert alert-danger", "This item failed its integrity check and cannot be opened."));
    } else if (item.decoy) {
      body.appendChild(el("div", "secret-text text-muted", "This was a decoy. There was nothing inside."));
    } else if (item.kind === "text") {
      body.appendChild(el("div", "secret-text", item.text));
      var copy = tip(el("button", "btn btn-secondary btn-sm me-2"), "Copy the text to the clipboard");
      copy.appendChild(el("i", "bi bi-clipboard me-1"));
      copy.appendChild(document.createTextNode("Copy"));
      copy.addEventListener("click", function () {
        navigator.clipboard.writeText(item.text).then(function () { alertBox("success", "Copied."); });
      });
      body.appendChild(copy);
    } else {
      body.appendChild(el("p", "", "Saved automatically as " + item.exported_path + " in the unlocked folder."));
      var dl = tip(el("button", "btn btn-primary btn-sm me-2"), "Decrypt the stored image and save it as a jpg");
      dl.appendChild(el("i", "bi bi-download me-1"));
      dl.appendChild(document.createTextNode("Save as JPG"));
      dl.addEventListener("click", function () { downloadImage(item); });
      body.appendChild(dl);
    }
    var del = tip(el("button", "btn btn-secondary btn-sm"), "Remove this item from the database for good");
    del.appendChild(el("i", "bi bi-trash3 me-1"));
    del.appendChild(document.createTextNode("Remove"));
    del.addEventListener("click", async function () {
      try { await apiJson("/api/items/" + item.id, { method: "DELETE" }); refresh(); }
      catch (e) { alertBox("danger", e.message); }
    });
    body.appendChild(del);
    c.appendChild(body);
    col.appendChild(c);
    return col;
  }

  function clockInfo(data) {
    if (data.clock_mode === "network_only") {
      return data.clock_source === "network"
        ? { icon: "bi-globe2", cls: "clock-network", text: "Time source: the internet (internet only mode)" }
        : { icon: "bi-wifi-off", cls: "clock-waiting", text: "Waiting for internet time. Nothing can be locked or unlocked until it is reachable." };
    }
    if (data.clock_source === "network") {
      return { icon: "bi-globe2", cls: "clock-network", text: "Time source: the internet" };
    }
    return { icon: "bi-shield-check", cls: "clock-system",
      text: "Time source: this computer's protected clock (no internet needed)" };
  }

  var lastClockText = "";
  var staleShown = false;
  var pollTimer = null;

  function staleBanner() {
    var box = el("div", "alert alert-warning");
    box.appendChild(el("i", "bi bi-arrow-clockwise me-2"));
    box.appendChild(document.createTextNode("Time Lock was restarted, so this page is out of date. "));
    var link = el("a", "alert-link", "Reload the page");
    link.href = "";
    link.addEventListener("click", function (ev) { ev.preventDefault(); location.reload(); });
    box.appendChild(link);
    $("statusBanners").replaceChildren(box);
  }

  function showClock(data) {
    var info = clockInfo(data);
    if (info.text === lastClockText) return;
    lastClockText = info.text;
    var badge = $("clockBadge");
    $("clockIcon").className = "bi " + info.icon;
    badge.className = "clock-badge ms-auto me-3 " + info.cls;
    badge.setAttribute("aria-label", info.text);
    badge.setAttribute("title", info.text);
    var t = bootstrap.Tooltip.getInstance(badge);
    if (t) t.setContent({ ".tooltip-inner": info.text });
  }

  var refreshing = false;

  async function refresh() {
    if (refreshing) return;               // never stack requests if one is slow
    refreshing = true;
    try { await refreshOnce(); } finally { refreshing = false; }
  }

  async function refreshOnce() {
    var data;
    try { data = await apiJson("/api/items"); }
    catch (e) {
      if (e.body && e.body.code === "bad_token") {
        // Time Lock was restarted since this page was opened. Stop asking, and say so once.
        clearInterval(pollTimer);
        if (!staleShown) { staleShown = true; staleBanner(); }
        return;
      }
      alertBox("danger", e.message); return;
    }
    serverOffset = data.now - Math.floor(Date.now() / 1000);
    showClock(data);
    banners(data);

    var upcoming = data.items.filter(function (i) { return i.state === "locked"; });
    var done = data.items.filter(function (i) { return i.state !== "locked"; });

    // ding for anything that was locked a moment ago and is now unlocked
    var nowLocked = upcoming.map(function (i) { return i.id; });
    if (knownLocked !== null) {
      done.filter(function (i) { return knownLocked.indexOf(i.id) !== -1; }).forEach(function (i) {
        log("unlocked", i.id);
        alertBox("success", "\"" + i.label + "\" has unlocked.");
        playDing();
      });
    }
    knownLocked = nowLocked;

    var signature = JSON.stringify(data.items.map(function (i) { return [i.id, i.state, i.b_concealed, i.unlock_at, i.seconds_remaining]; }));
    if (signature !== lastSignature) {
      lastSignature = signature;
      log("redraw", upcoming.length, "locked", done.length, "unlocked");
      disposeTooltips(document);
      $("upcomingBody").replaceChildren.apply($("upcomingBody"), upcoming.map(row));
      $("unlockedBody").replaceChildren.apply($("unlockedBody"), done.map(card));
      $("upcomingEmpty").classList.toggle("d-none", upcoming.length > 0);
      $("upcomingTable").classList.toggle("d-none", upcoming.length === 0);
      $("unlockedEmpty").classList.toggle("d-none", done.length > 0);
      $("panicBtn").classList.toggle("d-none", !done.some(function (i) { return i.state === "unlocked"; }));
      initTooltips(document);
    }
  }

  // ---------- calendar ----------
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December"];

  function startOfDay(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
  function unlockEpoch() { return Math.floor(selected.getTime() / 1000); }
  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function renderCalendar() {
    var box = $("calendar");
    box.replaceChildren();
    var head = el("div", "calendar-head");
    var prev = tip(el("button", "btn btn-secondary btn-sm"), "Previous month");
    prev.type = "button";
    prev.appendChild(el("i", "bi bi-chevron-left"));
    prev.addEventListener("click", function () { shiftMonth(-1); });
    var next = tip(el("button", "btn btn-secondary btn-sm"), "Next month");
    next.type = "button";
    next.appendChild(el("i", "bi bi-chevron-right"));
    next.addEventListener("click", function () { shiftMonth(1); });
    head.appendChild(prev);
    head.appendChild(el("span", "", MONTHS[viewMonth] + " " + viewYear));
    head.appendChild(next);
    box.appendChild(head);

    var grid = el("div", "calendar-grid");
    ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].forEach(function (d) { grid.appendChild(el("div", "calendar-dow", d)); });
    var offset = (new Date(viewYear, viewMonth, 1).getDay() + 6) % 7;
    for (var b = 0; b < offset; b++) grid.appendChild(el("div", "calendar-blank"));
    var days = new Date(viewYear, viewMonth + 1, 0).getDate();
    var today = startOfDay(new Date(Date.now() + serverOffset * 1000));
    for (var n = 1; n <= days; n++) {
      (function (day) {
        var date = new Date(viewYear, viewMonth, day);
        var btn = el("button", "calendar-day", String(day));
        btn.type = "button";
        btn.title = date.toDateString();
        if (date < today) btn.disabled = true;
        if (+date === +today) btn.classList.add("is-today");
        if (selected && +startOfDay(selected) === +date) btn.classList.add("is-selected");
        btn.addEventListener("click", function () {
          var picked = new Date(viewYear, viewMonth, day, selected.getHours(), selected.getMinutes(), selected.getSeconds());
          if (Math.floor(picked.getTime() / 1000) <= nowSec() + 30) {
            picked = new Date((nowSec() + 120) * 1000);   // today, time already passed: two minutes from now
          }
          setSelected(picked);
        });
        grid.appendChild(btn);
      })(n);
    }
    box.appendChild(grid);
  }

  function shiftMonth(delta) {
    viewMonth += delta;
    if (viewMonth < 0) { viewMonth = 11; viewYear--; }
    if (viewMonth > 11) { viewMonth = 0; viewYear++; }
    renderCalendar();
  }

  function markChoice() {
    document.querySelectorAll("#presets .btn").forEach(function (b) { b.classList.remove("is-active"); });
    var target = null;
    if (choice.type === "minutes") target = document.querySelector('#presets button[data-minutes="' + choice.value + '"]');
    else if (choice.type === "default") target = $("resetWeek");
    else if (choice.type === "random") target = $("randomBtn");
    if (target) target.classList.add("is-active");
  }

  function leaveRandom() {
    randomMode = false;
    $("calendar").classList.remove("is-random");
    $("timeRow").classList.remove("is-random");
  }

  function setSelected(date, newChoice) {
    leaveRandom();
    choice = newChoice || { type: "custom" };
    selected = date;
    viewYear = date.getFullYear();
    viewMonth = date.getMonth();
    $("unlockTime").value = pad(date.getHours()) + ":" + pad(date.getMinutes());
    renderCalendar();
    updateSummary();
    markChoice();
  }

  function enterRandom() {
    randomMode = true;
    choice = { type: "random" };
    $("calendar").classList.add("is-random");
    $("timeRow").classList.add("is-random");
    showAddError("");
    updateSummary();
    markChoice();
  }

  function updateSummary() {
    if (randomMode) {
      $("unlockSummary").textContent = "??? A surprise, somewhere within " + maxRandomDays + " days";
      $("unlockSummary").classList.remove("text-danger");
      $("unlockSummary").classList.add("text-accent");
      return;
    }
    var diff = unlockEpoch() - nowSec();
    var text = selected.toLocaleString(undefined,
      { weekday: "long", day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit", second: selected.getSeconds() ? "2-digit" : undefined });
    $("unlockSummary").textContent = diff > 0 ? text + "  (in " + remaining(diff) + ")" : text + "  (already passed)";
    $("unlockSummary").classList.toggle("text-danger", diff <= 0);
    $("unlockSummary").classList.toggle("text-accent", diff > 0);
    if (diff > 0) showAddError("");
  }

  function resetDefault() { setSelected(new Date((nowSec() + defaultSeconds) * 1000), { type: "default" }); }

  function showTab(name) {
    bootstrap.Tab.getOrCreateInstance($(name === "image" ? "tabImageBtn" : "tabTextBtn")).show();
  }

  function applyPrefs() {
    var p = loadPrefs();
    showTab(p.tab === "image" ? "image" : "text");
    $("deleteSource").checked = p.deleteSource !== false;
    var w = p.when || { type: "default" };
    if (w.type === "minutes" && w.value > 0) {
      setSelected(new Date((nowSec() + w.value * 60) * 1000), w);
    } else if (w.type === "random") {
      resetDefault();
      enterRandom();
    } else if (w.type === "custom" && typeof w.days === "number" && /^\d\d:\d\d$/.test(w.time || "")) {
      var base = startOfDay(new Date(nowSec() * 1000));
      var t = w.time.split(":");
      var d = new Date(base.getFullYear(), base.getMonth(), base.getDate() + w.days, +t[0], +t[1], 0);
      if (Math.floor(d.getTime() / 1000) > nowSec()) setSelected(d, w); else resetDefault();
    } else {
      resetDefault();
    }
    syncDeleteBox();
  }

  function rememberChoice(isImage) {
    var w = choice;
    if (w.type === "custom") {
      var days = Math.round((startOfDay(selected) - startOfDay(new Date(nowSec() * 1000))) / 86400000);
      w = { type: "custom", days: days, time: pad(selected.getHours()) + ":" + pad(selected.getMinutes()) };
    }
    var patch = { tab: isImage ? "image" : "text" };
    if (w.type !== "seconds") patch.when = w;
    if (isImage) patch.deleteSource = $("deleteSource").checked;
    savePrefs(patch);
  }

  function syncDeleteBox() {
    var hasPath = $("itemPath").value.trim() !== "";
    var hasFile = $("itemFile").files.length > 0;
    var box = $("deleteSource");
    if (hasFile && !hasPath) {
      box.disabled = true;
      $("deleteNote").textContent = "An uploaded picture cannot be deleted for you, because the browser keeps its location private. Use Browse instead.";
    } else {
      box.disabled = false;
      $("deleteNote").textContent = "Only works for a picture chosen with Browse or typed as a path.";
    }
  }

  // ---------- adding ----------
  function showAddError(text) {
    var box = $("addError");
    box.textContent = text;
    box.classList.toggle("d-none", !text);
  }

  async function submitAdd(force) {
    showAddError("");
    var isImage = $("tabImageBtn").classList.contains("active");
    var unlockAt = randomMode ? null : unlockEpoch();
    if (!randomMode) {
      if (unlockAt <= nowSec()) {
        showAddError("That time has already passed. Please pick a time in the future, or press one of the \"In 2 min\" buttons.");
        return;
      }
      if (unlockAt - nowSec() > LONG_SECONDS && !force) {
        $("longText").textContent = LONG_TEXT;
        modal("longModal").show();
        return;
      }
    } else if (maxRandomDays * 86400 > LONG_SECONDS && !force) {
      $("longText").textContent = "A random lock could last for up to " + maxRandomDays +
        " days, and nobody, not even you, will know when it opens. Are you sure?";
      modal("longModal").show();
      return;
    }
    var label = $("itemLabel").value;
    var btn = $("submitAdd");
    btn.disabled = true;
    try {
      var res;
      if (isImage) {
        var form = new FormData();
        var file = $("itemFile").files[0];
        var path = $("itemPath").value.trim();
        if (path) form.append("path", path);
        else if (file) form.append("file", file);
        form.append("label", label);
        if (randomMode) form.append("random", "true"); else form.append("unlock_at", String(unlockAt));
        form.append("confirm_long", force ? "true" : "false");
        form.append("delete_source", $("deleteSource").checked && path ? "true" : "false");
        res = await apiJson("/api/items/image", { method: "POST", body: form });
      } else {
        res = await postJson("/api/items/text", { text: $("itemText").value, label: label, confirm_long: !!force,
          unlock_at: unlockAt, random: randomMode });
      }
      log("locked", res.id);
      rememberChoice(isImage);
      modal("addModal").hide();
      $("itemText").value = ""; $("itemLabel").value = ""; $("itemFile").value = ""; $("itemPath").value = "";
      $("browsePanel").classList.add("d-none");
      syncDeleteBox();
      alertBox("success", randomMode
        ? "Locked safely away. Not even you know when it will open. Good luck!"
        : "Locked safely away. It cannot be opened until the unlock time.");
      if (res.source_deleted === true) alertBox("info", "The original picture was deleted from this computer.");
      else if (res.source_deleted === false) alertBox("warning", "The picture is locked, but the original could not be deleted. Please delete it yourself.");
      else if (isImage) alertBox("info", "The original picture was kept. You may wish to delete it yourself.");
      refresh();
    } catch (e) {
      showAddError(e.message);
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- folder browser ----------
  async function browseTo(path) {
    var data;
    try {
      data = await apiJson("/api/browse" + (path ? "?path=" + encodeURIComponent(path) : ""));
    } catch (e) {
      if (path) return browseTo(null);        // a remembered folder may be gone: start again from the beginning
      alertBox("danger", e.message);
      return;
    }
    savePrefs({ browseDir: data.path });
    var places = $("browsePlaces");
    places.replaceChildren();
    data.places.forEach(function (p) {
      var b = tip(el("button", "btn btn-secondary btn-sm"), p.note || p.path);
      b.type = "button";
      b.appendChild(el("i", "bi " + (PLACE_ICONS[p.name] || "bi-folder") + " me-1"));
      b.appendChild(document.createTextNode(p.name));
      b.addEventListener("click", function () { browseTo(p.path); });
      places.appendChild(b);
    });
    initTooltips(places);
    $("browsePath").textContent = data.path;
    var list = $("browseList");
    list.replaceChildren();
    function item(icon, name, onClick) {
      var a = el("button", "list-group-item list-group-item-action text-start");
      a.type = "button";
      a.appendChild(el("i", "bi " + icon + " me-2"));
      a.appendChild(document.createTextNode(name));
      a.addEventListener("click", onClick);
      list.appendChild(a);
    }
    var sep = data.path.indexOf("\\") !== -1 && data.path.indexOf("/") === -1 ? "\\" : "/";
    var join = function (name) { return data.path.replace(/[\\/]$/, "") + sep + name; };
    if (data.parent) item("bi-arrow-up", "Up one folder", function () { browseTo(data.parent); });
    data.dirs.forEach(function (d) { item("bi-folder-fill", d, function () { browseTo(join(d)); }); });
    data.files.forEach(function (f) {
      item("bi-image", f.name + "  (" + Math.max(1, Math.round(f.size / 1024)) + " KB)", function () {
        $("itemPath").value = join(f.name);
        $("itemFile").value = "";
        $("browsePanel").classList.add("d-none");
        if (!$("itemLabel").value) $("itemLabel").value = f.name.replace(/\.[^.]+$/, "");
        syncDeleteBox();
      });
    });
    if (!data.dirs.length && !data.files.length) list.appendChild(el("div", "list-group-item text-muted", "No folders or pictures here."));
  }

  // ---------- settings ----------
  async function loadSettings() {
    try { settings = await apiJson("/api/settings"); applyVolume(); maxRandomDays = settings.max_random_days; blindHint(); }
    catch (e) { log("settings unavailable", e.message); }
  }

  function blindHint() {
    var field = $("itemLabel");
    var text = settings.blind_mode
      ? "Blind mode is on: this name is encrypted and hidden until the item unlocks."
      : "A short name you will recognise. Do not put the secret here.";
    field.setAttribute("title", text);
    var t = bootstrap.Tooltip.getInstance(field);
    if (t) t.setContent({ ".tooltip-inner": text });
  }

  function fillSettings() {
    $("setTimeMode").value = settings.time_mode;
    $("setUrl").value = settings.network_url;
    $("setDefaultMinutes").value = settings.default_lock_minutes;
    $("setMaxRandom").value = settings.max_random_days;
    $("setBlind").checked = !!settings.blind_mode;
    $("setSound").checked = settings.sound_enabled;
    $("setVolume").value = settings.sound_volume;
    $("settingsError").classList.add("d-none");
  }

  async function saveSettings() {
    try {
      settings = await postJson("/api/settings", {
        time_mode: $("setTimeMode").value,
        network_url: $("setUrl").value.trim(),
        default_lock_minutes: parseInt($("setDefaultMinutes").value, 10),
        max_random_days: $("setMaxRandom").value,       // sent as typed: the server turns anything odd into 30
        sound_enabled: $("setSound").checked,
        sound_volume: parseInt($("setVolume").value, 10),
        blind_mode: $("setBlind").checked
      }, "PUT");
      defaultSeconds = settings.default_lock_minutes * 60;
      maxRandomDays = settings.max_random_days;
      blindHint();
      applyVolume();
      modal("settingsModal").hide();
      alertBox("success", "Settings saved.");
      refresh();
    } catch (e) {
      $("settingsError").textContent = e.message;
      $("settingsError").classList.remove("d-none");
    }
  }

  // ---------- extend ----------
  function resetExtendConfirm() {
    pendingExtend.needConfirm = false;
    $("confirmExtendText").textContent = "Lock longer";
    $("extendError").classList.add("d-none");
  }

  async function submitExtend(force) {
    try {
      await postJson("/api/items/" + pendingExtend.id + "/extend", { seconds: pendingExtend.seconds, confirm_long: !!force });
      modal("extendModal").hide();
      alertBox("success", "\"" + pendingExtend.label + "\" will stay locked longer.");
      refresh();
    } catch (e) {
      $("extendError").textContent = e.message;
      $("extendError").classList.remove("d-none");
      if (e.body && e.body.needs_confirm) {      // ask once more, inside this window
        pendingExtend.needConfirm = true;
        $("confirmExtendText").textContent = "Yes, I am sure";
      }
    }
  }

  // ---------- dares ----------
  var DARES = [
    { text: "Lock your most embarrassing note for a month.", minutes: 30 * 1440 },
    { text: "Write a letter to yourself and lock it for a whole year.", minutes: 365 * 1440 },
    { text: "Lock something you usually check every hour, and leave it alone for a day.", minutes: 1440 },
    { text: "Lock a thank-you to someone, and let fate decide when it opens.", random: true },
    { text: "Lock a promise to your future self for exactly one week.", minutes: 7 * 1440 },
    { text: "Lock a photo that makes you smile, for a surprise day.", random: true },
    { text: "Lock a goal for the next three months, then go and make it happen.", minutes: 90 * 1440 },
    { text: "Lock a compliment to yourself, to be opened tomorrow morning.", tomorrow: true },
    { text: "Lock a worry, and see how you feel about it in a week.", minutes: 7 * 1440 },
    { text: "Brave enough? Lock anything at all, and let fate pick the day.", random: true }
  ];
  var lastDare = -1;

  function dareMe() {
    var n;
    do { n = Math.floor(Math.random() * DARES.length); } while (n === lastDare && DARES.length > 1);
    lastDare = n;
    var d = DARES[n];
    if (d.random) {
      enterRandom();
    } else if (d.tomorrow) {
      var t = new Date((nowSec() + 86400) * 1000);
      setSelected(new Date(t.getFullYear(), t.getMonth(), t.getDate(), 8, 0, 0));
    } else {
      setSelected(new Date((nowSec() + d.minutes * 60) * 1000));
    }
    choice = { type: "seconds" };     // a dare is a one-off: never remembered as your usual choice
    markChoice();
    var note = $("dareNote");
    note.replaceChildren(el("i", "bi bi-dice-3-fill me-2"), document.createTextNode("Dare: " + d.text + " (the time is set for you)"));
    note.classList.remove("d-none");
    showAddError("");
  }

  // ---------- panic seal ----------
  var pendingSeal = null;

  function resetSealConfirm() {
    if (pendingSeal) pendingSeal.needConfirm = false;
    $("confirmSealText").textContent = "Seal everything";
    $("sealError").classList.add("d-none");
  }

  async function submitSeal(force) {
    try {
      var res = await postJson("/api/panic-seal", { seconds: pendingSeal.seconds, confirm_long: !!force });
      modal("sealModal").hide();
      alertBox("success", res.sealed + (res.sealed === 1 ? " item has" : " items have") + " been sealed again.");
      refresh();
    } catch (e) {
      $("sealError").textContent = e.message;
      $("sealError").classList.remove("d-none");
      if (e.body && e.body.needs_confirm) {
        pendingSeal.needConfirm = true;
        $("confirmSealText").textContent = "Yes, I am sure";
      }
    }
  }

  // ---------- wiring ----------
  document.addEventListener("DOMContentLoaded", function () {
    initTooltips(document);
    if (!TEST_MODE) $("testPresets").classList.add("d-none");

    $("openAdd").addEventListener("click", function () { showAddError(""); applyPrefs(); modal("addModal").show(); });
    $("resetWeek").addEventListener("click", resetDefault);
    $("randomBtn").addEventListener("click", enterRandom);
    document.querySelectorAll("#presets button[data-minutes]").forEach(function (b) {
      b.addEventListener("click", function () {
        var minutes = parseInt(b.dataset.minutes, 10);
        setSelected(new Date((nowSec() + minutes * 60) * 1000), { type: "minutes", value: minutes });
      });
    });
    document.querySelectorAll("#testPresets button").forEach(function (b) {
      b.addEventListener("click", function () {
        setSelected(new Date((nowSec() + parseInt(b.dataset.seconds, 10)) * 1000), { type: "seconds" });
      });
    });
    $("unlockTime").addEventListener("change", function () {
      var parts = $("unlockTime").value.split(":");
      if (parts.length === 2) {
        setSelected(new Date(selected.getFullYear(), selected.getMonth(), selected.getDate(), +parts[0], +parts[1], 0));
      }
    });
    $("submitAdd").addEventListener("click", function () { submitAdd(false); });
    $("confirmLong").addEventListener("click", function () { modal("longModal").hide(); submitAdd(true); });
    $("openBrowse").addEventListener("click", function () {
      var panel = $("browsePanel");
      panel.classList.toggle("d-none");
      if (!panel.classList.contains("d-none")) browseTo(loadPrefs().browseDir || null);
    });
    $("itemFile").addEventListener("change", function () {
      if ($("itemFile").files.length) $("itemPath").value = "";
      syncDeleteBox();
    });
    $("itemPath").addEventListener("input", syncDeleteBox);
    $("forgetPrefs").addEventListener("click", function () { forgetPrefs(); alertBox("info", "Remembered choices cleared."); });

    $("confirmReveal").addEventListener("click", async function () {
      modal("revealModal").hide();
      try {
        var res = await postJson("/api/items/" + pendingRevealId + "/reveal");
        alertBox("warning", "Revealed. It now unlocks on " + fmt(res.unlock_at) + ".");
      } catch (e) { alertBox("danger", e.message); }
      refresh();
    });

    document.querySelectorAll("#extendPresets button").forEach(function (b) {
      b.addEventListener("click", function () {
        resetExtendConfirm();
        pendingExtend.seconds = parseInt(b.dataset.seconds, 10);
        submitExtend(false);
      });
    });
    $("confirmExtend").addEventListener("click", function () {
      if (pendingExtend.needConfirm) { submitExtend(true); return; }
      pendingExtend.seconds = Math.round(parseFloat($("extendAmount").value) * parseInt($("extendUnit").value, 10));
      submitExtend(false);
    });
    ["extendAmount", "extendUnit"].forEach(function (id) {
      $(id).addEventListener("input", function () { if (pendingExtend) resetExtendConfirm(); });
    });

    $("dareBtn").addEventListener("click", dareMe);
    $("addModal").addEventListener("hidden.bs.modal", function () { $("dareNote").classList.add("d-none"); });
    ["itemText", "itemLabel", "unlockTime"].forEach(function (id) {
      $(id).addEventListener("input", function () { $("dareNote").classList.add("d-none"); });
    });

    $("panicBtn").addEventListener("click", function () {
      pendingSeal = { seconds: 86400, needConfirm: false };
      resetSealConfirm();
      modal("sealModal").show();
    });
    document.querySelectorAll("#sealPresets button").forEach(function (b) {
      b.addEventListener("click", function () {
        resetSealConfirm();
        pendingSeal.seconds = parseInt(b.dataset.seconds, 10);
        submitSeal(false);
      });
    });
    $("confirmSeal").addEventListener("click", function () {
      if (pendingSeal.needConfirm) { submitSeal(true); return; }
      pendingSeal.seconds = Math.round(parseFloat($("sealAmount").value) * parseInt($("sealUnit").value, 10));
      submitSeal(false);
    });
    ["sealAmount", "sealUnit"].forEach(function (id) {
      $(id).addEventListener("input", function () { if (pendingSeal) resetSealConfirm(); });
    });

    $("addDecoys").addEventListener("click", async function () {
      try {
        var res = await postJson("/api/decoys", { count: parseInt($("decoyCount").value, 10) });
        alertBox("success", res.added + (res.added === 1 ? " decoy lock was" : " decoy locks were") + " added.");
        refresh();
      } catch (e) {
        $("settingsError").textContent = e.message;
        $("settingsError").classList.remove("d-none");
      }
    });

    $("openSettings").addEventListener("click", function () { fillSettings(); modal("settingsModal").show(); });
    $("saveSettings").addEventListener("click", saveSettings);
    $("testSound").addEventListener("click", function () {
      settings.sound_volume = parseInt($("setVolume").value, 10);
      playDing(true);
    });

    loadSettings().then(refresh);
    pollTimer = setInterval(refresh, 3000);
  });
})();
