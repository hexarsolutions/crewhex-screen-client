/* CrewHex screen player v2 — ONE renderer, two transports.
 *
 *   remote  (default)  Hosted at app.crewhex.com/player. The browser pairs, keeps
 *                      its device token in localStorage and caches media in
 *                      Cache Storage. For Fire TV / Android TV / any browser.
 *   local              Inside crewhex-screen-client on a Raspberry Pi. The Python
 *                      client owns the token (0600 file), downloads + verifies
 *                      media to disk and serves everything on 127.0.0.1. The page
 *                      never sees a credential. Set window.CHX_TRANSPORT='local'.
 *
 * Contract: api/routers/signage.py (device v2). "What plays now" is resolver.js,
 * a mirror of api/signage_resolver.py. Page blocks are drawn with the same
 * geometry as kiosk v1.1.5 (which matches the 16:9 page-builder canvas).
 *
 * Rendering uses DOM APIs only (textContent / setAttribute). No innerHTML with
 * tenant data anywhere — v1 kiosk was injectable through image_url and colours.
 */
(function () {
  'use strict';
  var VERSION = '2.0.0';
  var LOCAL = window.CHX_TRANSPORT === 'local';
  var CACHE = 'chx-media-v1';
  var K = { token: 'chx.player.token', tokenAt: 'chx.player.tokenAt', manifest: 'chx.player.manifest' };
  var FAST = /^(localhost|127\.0\.0\.1)$/.test(location.hostname) && new URLSearchParams(location.search).has('fast');
  var MANIFEST_EVERY = LOCAL ? 3e3 : (FAST ? 2e3 : 30e3);    // local = reading a file from the Pi client
  var HEARTBEAT_EVERY = FAST ? 3e3 : (LOCAL ? 15e3 : 60e3);
  var LIVE_EVERY = FAST ? 5e3 : 60e3;
  var ROTATE_TOKEN_AFTER = 7 * 864e5;

  // ------------------------------------------------------------ small utils
  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} }
  function getJSON(k) { try { return JSON.parse(get(k) || 'null'); } catch (e) { return null; } }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function el(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
  function css(n, o) { for (var k in o) n.style[k] = o[k]; return n; }
  function safeColour(c, dflt) { return (typeof c === 'string' && /^#[0-9a-f]{3,8}$/i.test(c)) ? c : dflt; }

  var S = {
    token: LOCAL ? null : get(K.token), manifest: LOCAL ? null : getJSON(K.manifest), live: {},
    bootAt: Date.now(), lastOk: Date.now(), errors: [], plays: {}, current: null, resKey: '', gen: 0,
    blobUrls: [], idx: {}, link: 'green', status: {}
  };
  var root = document.getElementById('root');
  function note(e) { var m = String(e && e.message || e).slice(0, 200); S.errors.push(m); S.errors = S.errors.slice(-5); }
  function Unpaired() { this.message = 'unpaired'; }

  // ============================================================ transports
  function remoteApiBase() {
    var q = new URLSearchParams(location.search).get('api');
    if (q && /^https:\/\/([a-z0-9-]+\.)*crewhex\.com\/api\/v1$|^http:\/\/(127\.0\.0\.1|localhost):\d+\/api\/v1$/.test(q)) return q;
    if (/^(localhost|127\.0\.0\.1)$/.test(location.hostname)) return 'http://127.0.0.1:8000/api/v1';
    return 'https://api.crewhex.com/api/v1';
  }
  var API = LOCAL ? '' : remoteApiBase();

  async function call(url, opts) {
    opts = opts || {};
    var h = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    if (LOCAL) h['X-CrewHex-Kiosk'] = '1';
    else if (S.token && !opts.noAuth) h.Authorization = 'Bearer ' + S.token;
    var r = await fetch(url, { method: opts.method || 'GET', headers: h, cache: 'no-store',
      body: opts.body ? JSON.stringify(opts.body) : undefined });
    if (!LOCAL) S.lastOk = Date.now();
    if (r.status === 401 && !opts.noAuth && !LOCAL) throw new Unpaired();
    return r;
  }
  async function jsonOrThrow(r) {
    var j = null; try { j = await r.json(); } catch (e) {}
    if (!r.ok) { var e = new Error((j && j.detail && j.detail.error && j.detail.error.message) || ('HTTP ' + r.status)); e.status = r.status; throw e; }
    return j;
  }

  var Remote = {
    manifest: async function () {
      var h = {};
      if (S.manifest && S.manifest.etag) h['If-None-Match'] = '"' + S.manifest.etag + '"';
      var r = await call(API + '/hub-device/v2/manifest', { headers: h });
      if (r.status === 304) return null;
      return jsonOrThrow(r);
    },
    live: async function () { return (await jsonOrThrow(await call(API + '/hub-device/v2/live'))).live || {}; },
    report: async function (body) { return jsonOrThrow(await call(API + '/hub-device/v2/heartbeat', { method: 'POST', body: body })); },
    mediaSrc: function (id, m) { return blobUrl(mediaKey(id, m), m.url); },
    pageImageSrc: function (u) { return u.indexOf('/hub-device/') === 0 ? blobUrl(pageKey(u), u) : Promise.resolve(null); }
  };
  var Local = {
    manifest: async function () {
      var m = await jsonOrThrow(await call('/api/manifest'));
      if (!m || !m.etag || (S.manifest && S.manifest.etag === m.etag)) return null;
      return m;
    },
    live: async function () { return (await jsonOrThrow(await call('/api/live'))).live || {}; },
    report: async function (body) { return jsonOrThrow(await call('/api/report', { method: 'POST', body: body })); },
    // The Pi client already downloaded + checksum-verified these to disk.
    mediaSrc: function (id) { return Promise.resolve('/media/' + encodeURIComponent(id)); },
    pageImageSrc: function (u) {
      if (/^https:\/\//.test(u)) return Promise.resolve(u);          // v1-server fallback: signed URL
      var k = (u.split('key=')[1] || '');
      return Promise.resolve(k ? '/page-image?key=' + encodeURIComponent(decodeURIComponent(k)) : null);
    }
  };
  var T = LOCAL ? Local : Remote;

  // ============================================================ remote: pairing
  async function pairRemote() {
    S.manifest = null; set(K.manifest, null); set(K.token, null); S.token = null;
    show(pairingLayer(null, 'Connecting…', 0));
    var backoff = 3000;
    for (;;) {
      var reg;
      try {
        reg = await jsonOrThrow(await call(API + '/hub-device/v2/register', { method: 'POST', noAuth: true,
          body: { player_kind: playerKind(), client_version: VERSION } }));
        backoff = 3000;
      } catch (e) {
        show(pairingLayer(null, 'Waiting for network…', 0)); await sleep(backoff); backoff = Math.min(backoff * 2, 60000); continue;
      }
      var expires = new Date(reg.expires_at).getTime(), total = expires - Date.now();
      show(pairingLayer(reg.pairing_code, null, 1));
      while (Date.now() < expires) {
        await sleep((FAST ? 1 : (reg.poll_seconds || 3)) * 1000);
        updateBar((expires - Date.now()) / total);
        var r;
        try { r = await call(API + '/hub-device/v2/claim-status', { method: 'POST', noAuth: true,
          body: { registration_id: reg.registration_id, device_secret: reg.device_secret } }); }
        catch (e) { continue; }
        if (r.status === 404 || r.status === 409 || r.status === 410) break;
        if (!r.ok) continue;
        var j = await r.json();
        if (j.status === 'paired') { S.token = j.device_token; set(K.token, S.token); set(K.tokenAt, String(Date.now())); return; }
      }
    }
  }
  function playerKind() {
    var ua = navigator.userAgent;
    if (/AFT|Fire TV/i.test(ua)) return 'firetv';
    if (/Android/i.test(ua)) return 'android';
    if (/aarch64|armv7|Raspberry/i.test(ua)) return 'pi-browser';
    return 'web';
  }
  async function maybeRotate() {
    if (LOCAL) return;
    var at = +get(K.tokenAt) || 0;
    if (Date.now() - at < ROTATE_TOKEN_AFTER) return;
    var j = await jsonOrThrow(await call(API + '/hub-device/v2/rotate-token', { method: 'POST' }));
    S.token = j.device_token; set(K.token, S.token); set(K.tokenAt, String(Date.now()));
  }

  // ============================================================ local: status from the Pi client
  var wifiSkip = false;
  async function localStatus() {
    var st;
    try { st = await jsonOrThrow(await call('/api/status')); }
    catch (e) { S.link = 'red'; paintLink(); return null; }
    S.status = st; S.link = st.link || 'green'; paintLink();
    (st.commands || []).forEach(runCommand);
    return st;
  }
  var waitingPair = false;
  async function waitLocalPaired() {
    if (waitingPair) return; waitingPair = true;
    try { await waitLocalPairedInner(); } finally { waitingPair = false; }
  }
  async function waitLocalPairedInner() {
    for (;;) {
      var st = await localStatus();
      if (st && st.mode === 'content') return;
      if (st && st.mode === 'pairing') {
        var online = st.last_ok && (Date.now() / 1000 - st.last_ok < 60);
        if (!online && !wifiSkip && st.wifi_available) { await wifiSetup(); continue; }
        if (!document.querySelector('.pair .code') || document.querySelector('.pair').dataset.code !== st.pairing_code) {
          show(st.pairing_code ? pairingLayer(st.pairing_code, null, 1, st.pairing_expires_at) : pairingLayer(null, online ? 'Getting a pairing code…' : 'Waiting for network…', 0));
        }
        if (st.pairing_expires_at) updateBar((new Date(st.pairing_expires_at) - Date.now()) / (15 * 60e3));
      }
      await sleep(FAST ? 700 : 2000);
    }
  }

  // Wi-Fi onboarding (Pi only). Talks to the local client, which drives nmcli.
  async function wifiSetup() {
    var L = el('div', 'layer setup');
    L.appendChild(el('div', 'who', 'CrewHex screen'));
    var mid = el('div', 'mid'); L.appendChild(mid);
    mid.appendChild(el('h1', null, 'Connect this screen to the internet'));
    mid.appendChild(el('p', null, 'Pick your Wi-Fi network. Using a network cable? Skip this.'));
    var list = el('div', 'nets'); list.appendChild(el('div', 'net', 'Scanning…')); mid.appendChild(list);
    var pwRow = el('div', 'pwrow'); pwRow.style.display = 'none';
    var pw = el('input'); pw.type = 'password'; pw.placeholder = 'Wi-Fi password'; pw.autocomplete = 'off';
    var go = el('button', null, 'Join'); pwRow.appendChild(pw); pwRow.appendChild(go); mid.appendChild(pwRow);
    var msg = el('div', 'setupnote'); mid.appendChild(msg);
    var skip = el('button', 'linkbtn', 'Skip, show the pairing code'); mid.appendChild(skip);
    show(L);
    var chosen = null;
    return new Promise(function (done) {
      skip.onclick = function () { wifiSkip = true; done(); };
      function join() {
        if (!chosen) return;
        go.disabled = true; msg.textContent = 'Joining ' + chosen + '…';
        call('/api/wifi/connect', { method: 'POST', body: { ssid: chosen, password: pw.value } }).then(jsonOrThrow).then(function (d) {
          if (d.reachable) { msg.textContent = 'Connected.'; setTimeout(done, 1200); }
          else msg.textContent = d.ok ? 'Joined, but CrewHex isn\u2019t reachable yet. Retrying…' : 'Couldn\u2019t join. Check the password and try again.';
        }).catch(function (e) { msg.textContent = 'Couldn\u2019t join: ' + e.message; }).then(function () { go.disabled = false; });
      }
      go.onclick = join; pw.addEventListener('keydown', function (e) { if (e.key === 'Enter') join(); });
      call('/api/wifi/scan').then(jsonOrThrow).then(function (d) {
        list.textContent = '';
        if (!d.networks.length) { list.appendChild(el('div', 'net', 'No networks found. Move the screen closer to the router or use a cable.')); return; }
        d.networks.forEach(function (n) {
          var row = el('button', 'net'); row.appendChild(el('span', null, n.ssid + (n.secure ? '  \uD83D\uDD12' : '')));
          row.appendChild(el('span', 'sig', (n.signal || '') + '%'));
          row.onclick = function () { chosen = n.ssid; pwRow.style.display = 'flex'; pw.focus(); msg.textContent = ''; };
          list.appendChild(row);
        });
      }).catch(function (e) { list.textContent = ''; list.appendChild(el('div', 'net', 'Scan failed: ' + e.message)); });
    });
  }

  function pairingLayer(code, status, pct) {
    var L = el('div', 'layer pair'); if (code) L.dataset.code = code;
    L.appendChild(el('div', 'who', 'CrewHex screen'));
    var mid = el('div', 'mid');
    if (code) {
      mid.appendChild(el('h1', null, 'Pair this screen'));
      var c = el('div', 'code');
      code.replace(/\s/g, '').split('').forEach(function (ch, i) { if (i === 3) c.appendChild(el('i')); c.appendChild(el('span', null, ch)); });
      mid.appendChild(c);
      var p = el('p');
      p.appendChild(document.createTextNode('On a computer, sign in to CrewHex as an admin, open '));
      p.appendChild(el('b', null, 'Screens'));
      p.appendChild(document.createTextNode(', choose '));
      p.appendChild(el('b', null, 'Add screen'));
      p.appendChild(document.createTextNode(' and type this code.'));
      mid.appendChild(p);
    } else {
      mid.appendChild(el('h1', null, status));
    }
    L.appendChild(mid);
    var f = el('div', 'foot');
    var bar = el('div', 'bar'); var fill = el('div'); fill.id = 'pairbar'; fill.style.width = (pct * 100) + '%'; bar.appendChild(fill);
    f.appendChild(el('span', null, code ? 'A new code appears when this one expires' : ''));
    f.appendChild(bar);
    f.appendChild(el('span', null, 'v' + VERSION + (LOCAL ? ' · Pi' : '')));
    L.appendChild(f);
    return L;
  }
  function updateBar(p) { var b = document.getElementById('pairbar'); if (b) b.style.width = Math.max(0, Math.min(1, p)) * 100 + '%'; }

  // ============================================================ remote: media cache
  function mediaKey(id, m) { return 'https://cache.crewhex.local/media/' + id + '/' + m.sha256; }
  function pageKey(url) { return 'https://cache.crewhex.local/page/' + encodeURIComponent(url); }
  var hasCache = !LOCAL && typeof caches !== 'undefined';
  async function sha256Hex(buf) {
    if (!(window.crypto && crypto.subtle)) return null;
    var d = await crypto.subtle.digest('SHA-256', buf);
    return Array.prototype.map.call(new Uint8Array(d), function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
  }
  async function fetchAuthed(relUrl) {
    var r = await fetch(API + relUrl, { headers: { Authorization: 'Bearer ' + S.token }, cache: 'no-store' });
    if (r.status === 401) throw new Unpaired();
    if (!r.ok) throw new Error('download ' + r.status + ' ' + relUrl);
    return r;
  }
  function pageImageUrls(man) {
    var out = [];
    Object.keys(man.pages || {}).forEach(function (k) {
      (man.pages[k].blocks || []).forEach(function (b) { if (b.type === 'image' && b.image_url && b.image_url.indexOf('/hub-device/') === 0) out.push(b.image_url); });
    });
    return out;
  }
  async function prefetch(man) {
    if (!hasCache) return;
    var cache = await caches.open(CACHE), want = {};
    var ids = Object.keys(man.media || {});
    for (var i = 0; i < ids.length; i++) {
      var id = ids[i], m = man.media[id], key = mediaKey(id, m); want[key] = 1;
      if (await cache.match(key)) continue;
      try {
        var r = await fetchAuthed(m.url), buf = await r.arrayBuffer();
        var h = await sha256Hex(buf);
        if (h && h !== m.sha256) throw new Error('checksum mismatch for media ' + id);
        await cache.put(key, new Response(buf, { headers: { 'Content-Type': m.content_type } }));
      } catch (e) { if (e instanceof Unpaired) throw e; note(e); }
    }
    var imgs = pageImageUrls(man);
    for (var j = 0; j < imgs.length; j++) {
      var pk = pageKey(imgs[j]); want[pk] = 1;
      if (await cache.match(pk)) continue;
      try { var r2 = await fetchAuthed(imgs[j]); await cache.put(pk, new Response(await r2.blob(), { headers: { 'Content-Type': r2.headers.get('Content-Type') || 'image/*' } })); }
      catch (e2) { if (e2 instanceof Unpaired) throw e2; note(e2); }
    }
    (await cache.keys()).forEach(function (req) { if (!want[req.url]) cache.delete(req); });
  }
  async function blobUrl(cacheKey, fallbackRel) {
    var resp = null;
    if (hasCache) { var c = await caches.open(CACHE); resp = await c.match(cacheKey); }
    if (!resp && fallbackRel) resp = await fetchAuthed(fallbackRel);
    if (!resp) return null;
    var u = URL.createObjectURL(await resp.blob()); S.blobUrls.push(u); return u;
  }
  function releaseBlobs() { S.blobUrls.splice(0).forEach(function (u) { URL.revokeObjectURL(u); }); }

  // ============================================================ sync loops
  async function syncManifest() {
    var man = await T.manifest();
    if (!man) return false;
    await prefetch(man);                      // remote: download everything BEFORE switching
    S.manifest = man; if (!LOCAL) set(K.manifest, JSON.stringify(man));
    applyDisplay(man);
    return true;
  }
  async function syncLive() { S.live = await T.live(); paintLive(); }
  async function heartbeat() {
    var est = null;
    try { est = navigator.storage && navigator.storage.estimate ? await navigator.storage.estimate() : null; } catch (e) {}
    var plays = Object.keys(S.plays).map(function (k) { var p = S.plays[k]; return { day: p.day, ref: p.ref, plays: p.plays, seconds: Math.round(p.seconds) }; });
    var j = await T.report({
      client_version: VERSION, manifest_etag: S.manifest && S.manifest.etag, mode: S.resKey.split('|')[0],
      current_item: S.current, uptime_s: Math.round((Date.now() - S.bootAt) / 1000), cache_bytes: est && est.usage,
      screen_w: screen.width, screen_h: screen.height, user_agent: navigator.userAgent.slice(0, 160),
      errors: S.errors, plays: plays });
    S.plays = {}; S.errors = [];
    (j.commands || []).forEach(runCommand);
    if (!LOCAL && j.manifest_etag && S.manifest && j.manifest_etag !== S.manifest.etag) syncManifest().catch(guard);
  }
  function runCommand(c) {
    if (c.command === 'reload') location.reload();
    else if (c.command === 'identify') identify();
    else if (c.command === 'clear_cache') {
      (hasCache ? caches.delete(CACHE) : Promise.resolve()).then(function () { set(K.manifest, null); location.reload(); });
    }
  }
  function identify() {
    var o = document.getElementById('ident');
    o.firstChild.textContent = (S.manifest && S.manifest.display.name) || 'This screen';
    o.style.display = 'grid'; setTimeout(function () { o.style.display = 'none'; }, 15000);
  }
  function guard(e) { if (e instanceof Unpaired) { restartPairing(); return; } note(e); }
  var repairing = false;
  function restartPairing() {
    if (LOCAL || repairing) return; repairing = true; S.gen++;
    (hasCache ? caches.delete(CACHE) : Promise.resolve()).then(pairRemote)
      .then(function () { repairing = false; return syncManifest(); }).catch(guard);
  }
  function every(ms, fn) {
    var run = function () { if (repairing || (!LOCAL && !S.token) || (LOCAL && S.status.mode !== 'content')) return; fn().catch(guard); };
    setInterval(run, ms); run();
  }

  // ============================================================ link status
  function paintLink() {
    var d = document.getElementById('net');
    if (!d) return;
    var lvl = LOCAL ? S.link : (S.token && Date.now() - S.lastOk > 180e3 ? 'red' : (S.token && Date.now() - S.lastOk > 90e3 ? 'amber' : 'green'));
    d.className = lvl; d.style.display = lvl === 'green' ? 'none' : 'flex';
    d.lastChild.textContent = lvl === 'red' ? 'Offline — playing saved content' : 'Server not responding';
  }

  // ============================================================ display settings
  function applyDisplay(man) {
    var o = (man.display && man.display.orientation) || 'landscape';
    root.className = o === 'landscape' ? '' : o;
    document.documentElement.style.setProperty('--brand', safeColour(man.branding && man.branding.primary_colour, '#E9653A'));
  }

  // ============================================================ playback
  function show(layer) {
    var old = Array.prototype.slice.call(root.children);
    root.appendChild(layer);
    requestAnimationFrame(function () { layer.classList.add('on'); });
    setTimeout(function () { old.forEach(function (n) { if (n !== layer) n.remove(); }); }, 500);
  }
  function banner(text, tone) {
    var b = document.getElementById('banner');
    if (!text) { b.style.display = 'none'; return; }
    // rolling marquee: NOTICE tag + text repeated so it loops seamlessly
    b.className = tone === 'danger' ? 'danger' : '';
    b.style.background = '';
    b.innerHTML = '';
    if (tone !== 'danger') {
      var pc = S.manifest && S.manifest.display && S.manifest.display.primary_colour;
      if (pc) b.style.background = safeColour(pc, '');
    }
    var tag = el('div', 'btag', 'NOTICE'); b.appendChild(tag);
    var roll = el('div', 'broll'); var track = el('div', 'btrack');
    for (var k = 0; k < 4; k++) { track.appendChild(el('i')); track.appendChild(el('span', null, text)); }
    roll.appendChild(track); b.appendChild(roll);
    b.style.display = 'block';
  }
  function countPlay(ref, seconds) {
    var d = new Date(), day = d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2);
    var k = day + '|' + ref, p = S.plays[k] || (S.plays[k] = { day: day, ref: ref, plays: 0, seconds: 0 });
    p.plays++; p.seconds += seconds;
  }
  function resolution() {
    var man = S.manifest, now = new Date(), scr = Object.assign({}, man.display);
    var r = ChxResolver.resolve(scr, man.schedules, man.overrides, now);
    if (r.mode === 'after_hours' && (scr.after_hours_mode === 'keep_playing' || scr.after_hours_mode === 'content')) {
      r = ChxResolver.resolve(Object.assign({}, scr, { always_on: true }), man.schedules, man.overrides, now);
    }
    return r;
  }
  function keyOf(r) { return [r.mode, r.playlist_id || '', r.message || ''].join('|'); }

  function itemsFor(r) {
    var man = S.manifest, now = new Date();
    if (r.mode === 'legacy_pages' || (r.mode === 'after_hours' && man.display.after_hours_mode === 'page')) {
      var keys = Object.keys(man.pages || {});
      if (r.mode === 'after_hours') keys = keys.filter(function (k) { return k === man.display.after_hours_page_key; });
      else keys = keys.filter(function (k) { return man.pages[k].enabled; });
      keys.sort(function (a, b) { return (man.pages[a].sort_order || 0) - (man.pages[b].sort_order || 0); });
      return keys.map(function (k) { return { id: 'page:' + k, kind: 'hub_page', page_key: k, duration_seconds: man.pages[k].rotation_seconds || 30 }; });
    }
    var pl = r.playlist_id && man.playlists[r.playlist_id];
    if (!pl) return [];
    return pl.items.filter(function (i) {
      if (!ChxResolver.itemLive(i, now)) return false;
      if (i.kind === 'media') return !!man.media[i.media_id];
      if (i.kind === 'hub_page') return !!man.pages[i.page_key];
      return true;
    });
  }

  async function play() { for (;;) { try { await playLoop(); } catch (e) { guard(e); await sleep(3000); } } }
  async function playLoop() {
    for (;;) {
      var myGen = S.gen;
      if (repairing || (!LOCAL && !S.token) || (LOCAL && S.status.mode !== 'content')) { await sleep(1000); continue; }
      if (!S.manifest) { show(messageLayer({ title: 'Waiting for content', body: 'This screen is paired. Content appears once it is published.' }, 'info')); await waitChange(myGen, 5000); continue; }
      var r = resolution(); S.resKey = keyOf(r);
      banner(r.mode === 'override' && r.playlist_id && r.message ? r.message : '', r.tone);
      var ahm = S.manifest.display.after_hours_mode || 'blank';
      if (r.mode === 'off' || (r.mode === 'after_hours' && ahm === 'blank')) {
        S.current = r.mode; show(el('div', 'layer blank')); await waitChange(myGen, 30000); continue;
      }
      if (r.mode === 'after_hours' && ahm === 'message') {
        S.current = 'after_hours'; show(messageLayer({ title: S.manifest.display.after_hours_message || 'Closed', body: '' }, 'info'));
        await waitChange(myGen, 30000); continue;
      }
      if (r.mode === 'override' && !r.playlist_id) {
        // Text-only broadcast = rolling banner over the normal programme.
        // Fall through to the default items below; the banner stays up.
        r.mode = 'default'; S.resKey = keyOf(r);
      }
      var items = itemsFor(r);
      if (!items.length) {
        S.current = 'empty';
        show(messageLayer({ title: 'Nothing to show yet', body: 'Publish Hub pages or schedule a playlist for this screen in CrewHex.' }, 'info'));
        await waitChange(myGen, 15000); continue;
      }
      if ((S.manifest && S.manifest.shoutouts || []).length && S.current !== 'recognition') {
        items = items.concat([{ id: 'recognition', kind: 'recognition', secs: 10 }]);
      }
      var listKey = S.resKey, i = (S.idx[listKey] || 0) % items.length;
      S.idx[listKey] = i + 1;
      var item = items[i];
      S.current = item.id;
      var t0 = Date.now();
      try { await playItem(item, myGen); } catch (e) { guard(e); await sleep(1000); }
      countPlay(item.id, (Date.now() - t0) / 1000);
    }
  }
  // Resolves after `ms`, or early when the schedule result or manifest changes.
  function waitChange(gen, ms) {
    return new Promise(function (done) {
      var start = Date.now(), key = S.resKey, et = S.manifest && S.manifest.etag;
      var t = setInterval(function () {
        var changed = S.gen !== gen || (S.manifest && keyOf(resolution()) !== key) ||
          (key === '' && S.manifest && S.manifest.etag !== et);
        if (changed || Date.now() - start >= ms) { clearInterval(t); done(); }
      }, 1000);
    });
  }

  async function playItem(item, gen) {
    var L, dur = Math.max(3, item.duration_seconds || 15) * 1000;
    if (item.kind === 'media') {
      var m = S.manifest.media[item.media_id];
      var u = await T.mediaSrc(item.media_id, m);
      L = el('div', 'layer media' + ((item.config && item.config.fit) === 'cover' ? ' cover' : ''));
      if (m.kind === 'video') {
        var v = el('video'); v.muted = true; v.autoplay = true; v.playsInline = true; v.src = u; L.appendChild(v);
        show(L);
        await new Promise(function (done) {
          var cap = setTimeout(done, 4 * 3600e3);
          v.addEventListener('loadedmetadata', function () { clearTimeout(cap); cap = setTimeout(done, (v.duration || dur / 1000) * 1000 + 2000); });
          v.addEventListener('ended', function () { clearTimeout(cap); done(); });
          v.addEventListener('error', function () { clearTimeout(cap); note('video error ' + item.media_id); done(); });
          var p = v.play(); if (p && p.catch) p.catch(function (e) { note(e); });
        });
        releaseBlobs(); return;
      }
      var img = el('img'); img.alt = ''; img.src = u; L.appendChild(img);
    } else if (item.kind === 'hub_page') {
      var pg = S.manifest.pages[item.page_key];
      L = await pageLayer(pg);
      if (!item.duration_seconds) dur = (pg.rotation_seconds || 30) * 1000;
    } else if (item.kind === 'web_url') {
      L = el('div', 'layer web');
      var f = el('iframe');
      f.setAttribute('sandbox', 'allow-scripts allow-same-origin');
      f.setAttribute('referrerpolicy', 'no-referrer');
      f.setAttribute('allow', 'autoplay');
      f.src = item.url; L.appendChild(f);
    } else if (item.kind === 'recognition') {
      L = recognitionLayer(); dur = 10e3;
    } else if (item.kind === 'message') {
      L = messageLayer(item.config || {}, (item.config && item.config.tone) || 'info');
    } else { return; }
    show(L);
    await waitChange(gen, dur);
    setTimeout(releaseBlobs, 800);
  }

  function recognitionLayer() {
    // Recent staff shoutouts (recognition notices) as a slide.
    var L = el('div', 'layer page'), st = el('div', 'stage'); L.appendChild(st);
    var sl = el('div', 'slide');
    sl.appendChild(el('h2', null, 'Recognition'));
    var items = (S.manifest && S.manifest.shoutouts || []).slice(0, 3);
    items.forEach(function (so) {
      var card = el('div', 'reco');
      var who = el('div', 'reco-who'); who.appendChild(el('b', null, so.to_name));
      who.appendChild(document.createTextNode(' — recognised by ' + so.from_name));
      card.appendChild(who);
      card.appendChild(el('div', 'reco-msg', so.message));
      sl.appendChild(card);
    });
    st.appendChild(sl);
    return L;
  }

  function messageLayer(cfg, tone) {
    var L = el('div', 'layer msg ' + (tone || 'info'));
    if (cfg.title) L.appendChild(el('h2', null, cfg.title));
    if (cfg.body) L.appendChild(el('p', null, cfg.body));
    return L;
  }

  // ------------------------------------------------------------ Hub page renderer
  // Geometry and sizes follow kiosk v1.1.5 so pages look exactly as tenants
  // designed them. vw/vh there == cqw/cqh of the 16:9 stage here.
  function keepBlock(b) {
    if (!b) return false;
    if (b.type === 'image') return !!b.image_url;
    if (b.type === 'text' || b.type === 'heading') return !!String(b.text || '').trim();
    return true;
  }
  function hasGeom(b) { return ['x', 'y', 'w', 'h'].every(function (k) { return isFinite(Number(b[k])) && b[k] !== null && b[k] !== ''; }); }
  function brandName() { return (S.manifest.branding && S.manifest.branding.name) || ''; }

  function statsNode(b, stacked) {
    var s = el('div', 'stats-row'); if (!stacked) css(s, { width: '100%', height: '100%', alignItems: 'center', justifyContent: 'space-around' });
    var live = S.live || {};
    function stat(label, key) { var d = el('div'); d.appendChild(el('div', 'stat-l', label)); var v = el('div', 'stat-v', liveText(key, live)); v.dataset.live = key; d.appendChild(v); s.appendChild(d); }
    if (b.show_fuel_dollars !== false) stat('FUEL THIS MONTH', 'fuel_dollars');
    if (b.show_fuel_litres !== false) stat('LITRES', 'fuel_litres');
    if (b.show_vehicles) stat('SERVICE DUE', 'vehicles_due');
    return s;
  }
  function liveText(key, live) {
    var v = live[key];
    if (key === 'fuel_dollars') return '$' + (v != null ? Number(v).toLocaleString('en-AU') : '—');
    if (key === 'fuel_litres') return (v != null ? Number(v).toLocaleString('en-AU') : '—') + ' L';
    return v != null ? String(v) : '—';
  }
  // Live numbers update in place (as kiosk v1.1.5 did) — no page rebuild, no flash.
  function paintLive() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-live]'), function (n) { n.textContent = liveText(n.dataset.live, S.live || {}); });
  }
  function clockNode() {
    var c = el('span'); c.style.fontVariantNumeric = 'tabular-nums';
    var tz = (S.manifest.display && S.manifest.display.timezone) || 'Australia/Brisbane';
    var tick = function () { if (c.isConnected) c._shown = 1; else if (c._shown) return clearInterval(h);
      try { c.textContent = new Date().toLocaleTimeString('en-AU', { timeZone: tz, hour: '2-digit', minute: '2-digit' }); } catch (e) {} };
    var h = setInterval(tick, 1000); tick(); return c;
  }
  function footerNode(b, stacked) {
    var f = el('div', 'foot'); css(f, { display: 'flex', justifyContent: 'space-between', width: '100%', alignItems: 'center' });
    if (!stacked) f.style.height = '100%';
    f.appendChild(el('span', null, brandName()));
    if (b.show_clock !== false) f.appendChild(clockNode());
    return f;
  }
  function listNode(b, stacked) {
    var l = el('div', 'list'); if (!stacked) css(l, { width: '100%', height: '100%', maxWidth: 'none', justifyContent: 'center' });
    (b.items || []).forEach(function (it) { var r = el('div', 'row'); r.appendChild(el('span', 't', String(it))); l.appendChild(r); });
    return l;
  }
  async function imageNode(b, stacked) {
    var src = await T.pageImageSrc(String(b.image_url || ''));
    if (!src) return null;
    var im = el('img'); im.alt = ''; im.src = src;
    if (stacked) css(im, { maxWidth: '78%', maxHeight: '62cqh', borderRadius: '16px', objectFit: 'contain' });
    else css(im, { width: '100%', height: '100%', borderRadius: '.6cqw', objectFit: 'cover' });
    return im;
  }
  async function blockNode(b, stacked) {
    if (b.type === 'heading') {
      var h = el('div', stacked ? 'hd-stack' : 'hd-box', b.text || ''); return h;
    }
    if (b.type === 'text') { var t = el('div', 'msgtxt', b.text || ''); if (!stacked) css(t, { width: '100%', height: '100%', overflow: 'hidden' }); return t; }
    if (b.type === 'image') return imageNode(b, stacked);
    if (b.type === 'list') return listNode(b, stacked);
    if (b.type === 'stats') return statsNode(b, stacked);
    if (b.type === 'footer') return footerNode(b, stacked);
    return null;
  }
  async function pageLayer(page) {
    var L = el('div', 'layer page'), st = el('div', 'stage'); L.appendChild(st);
    var blocks = Array.isArray(page.blocks) ? page.blocks.filter(keepBlock) : [];
    var legacy = String(page.message || '').trim();
    if (blocks.length && legacy && !blocks.some(function (b) { return b.type === 'text' || b.type === 'list' || b.type === 'image'; })) {
      blocks.push({ type: 'text', text: legacy });
    }
    if (!blocks.length) {                                     // legacy template page
      var sl = el('div', 'slide');
      sl.appendChild(el('h2', null, page.title || ''));
      if (legacy) sl.appendChild(el('div', 'msgtxt', legacy));
      var items = page.items || [];
      if (items.length) { var l = el('div', 'list'); items.forEach(function (it) { var r = el('div', 'row'); r.appendChild(el('span', 't', it.title || it.name || '')); r.appendChild(el('span', 's', it.subtitle || it.detail || '')); l.appendChild(r); }); sl.appendChild(l); }
      sl.appendChild(el('div', 'foot', page.title + (brandName() ? ' · ' + brandName() : '')));
      st.appendChild(topBar()); st.appendChild(sl); return L;
    }
    if (!blocks.some(hasGeom)) {                              // older stacked pages
      var col = el('div', 'slide');
      for (var i = 0; i < blocks.length; i++) { var n = await blockNode(blocks[i], true); if (n) col.appendChild(n); }
      st.appendChild(topBar()); st.appendChild(col); return L;
    }
    var stacked = [];                                         // canvas pages (page builder)
    for (var j = 0; j < blocks.length; j++) {
      var b = blocks[j];
      if (!hasGeom(b)) { stacked.push(b); continue; }
      var x = Math.max(0, Math.min(100, +b.x)), y = Math.max(0, Math.min(100, +b.y));
      var w = Math.max(1, Math.min(100 - x, +b.w)), hh = Math.max(1, Math.min(100 - y, +b.h));
      var sc = Math.max(.5, Math.min(3, (Number(b.font_scale) || 100) / 100));
      var box = css(el('div', 'blk'), { left: x + '%', top: y + '%', width: w + '%', height: hh + '%' });
      var inner = css(el('div'), { width: (100 / sc) + '%', height: (100 / sc) + '%', transform: 'scale(' + sc + ')', transformOrigin: 'top left' });
      var node = await blockNode(b, false); if (node) inner.appendChild(node);
      box.appendChild(inner); st.appendChild(box);
    }
    if (stacked.length) {
      var rest = css(el('div'), { position: 'absolute', inset: '6cqh 8cqw', display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: '3.2cqh' });
      for (var k = 0; k < stacked.length; k++) { var n2 = await blockNode(stacked[k], true); if (n2) rest.appendChild(n2); }
      st.appendChild(rest);
    }
    return L;
  }
  function topBar() {
    var t = el('div', 'top'), br = S.manifest.branding || {};
    if (br.logo_url && /^https:\/\//.test(br.logo_url)) { var i = el('img', 'mark'); i.alt = ''; i.src = br.logo_url; t.appendChild(i); }
    t.appendChild(el('span', 'name', S.manifest.display.name || br.name || ''));
    t.appendChild(css(clockNode(), { marginLeft: 'auto' }));
    return t;
  }

  // ============================================================ housekeeping + boot
  setInterval(function () {
    paintLink();
    var h = new Date().getHours();   // nightly refresh: new player builds + frees memory
    if (h === 3 && Date.now() - S.bootAt > 6 * 3600e3) location.reload();
  }, LOCAL ? 5e3 : 30e3);
  if (!LOCAL && 'serviceWorker' in navigator && location.protocol === 'https:') navigator.serviceWorker.register('sw.js').catch(function () {});
  document.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  async function boot() {
    if (LOCAL) {
      await waitLocalPaired();
      setInterval(function () { localStatus().then(function (st) {
        if (st && st.mode !== 'content') { S.manifest = null; S.gen++; waitLocalPaired(); }
      }); }, FAST ? 1000 : 5000);
    } else if (!S.token) {
      await pairRemote();
    }
    if (S.manifest) applyDisplay(S.manifest);
    every(MANIFEST_EVERY, syncManifest);
    every(HEARTBEAT_EVERY, heartbeat);
    every(LIVE_EVERY, syncLive);
    if (!LOCAL) every(3600e3, maybeRotate);
    play();
  }
  if (FAST) window.__chx = { state: function () { return { mode: S.resKey, current: S.current, etag: S.manifest && S.manifest.etag }; } };
  boot();
})();
