/* CrewHex signage resolver — JS mirror of api/signage_resolver.py.
 * Same rules, same order. If you change one, change both and run:
 *   node web/public/player/resolver.test.cjs
 *   pytest api/tests/test_signage_resolver.py
 */
(function (root) {
  'use strict';
  var TARGET_RANK = { screen: 3, group: 2, tenant: 1 };
  var WD = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };

  function hm(s) { var p = String(s || '00:00').split(':'); return (+p[0]) * 60 + (+p[1] || 0); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }

  // Wall-clock parts of `date` in IANA zone `tz`.
  function localParts(date, tz) {
    var f;
    try {
      f = new Intl.DateTimeFormat('en-US', { timeZone: tz || 'Australia/Brisbane', hourCycle: 'h23',
        year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', weekday: 'short' });
    } catch (e) {
      f = new Intl.DateTimeFormat('en-US', { timeZone: 'Australia/Brisbane', hourCycle: 'h23',
        year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', weekday: 'short' });
    }
    var o = {};
    f.formatToParts(date).forEach(function (p) { o[p.type] = p.value; });
    var y = +o.year, m = +o.month, d = +o.day;
    return { y: y, m: m, d: d, minute: (+o.hour % 24) * 60 + (+o.minute), day: WD[o.weekday],
             iso: y + '-' + pad(m) + '-' + pad(d) };
  }
  function yesterdayDay(lp) { return (lp.day + 6) % 7; }

  function inWindow(lp, days, start, end) {
    var s = hm(start), e = hm(end), set = {}, i;
    for (i = 0; i < (days || []).length; i++) set[days[i]] = true;
    if (s === e) return !!set[lp.day];
    if (s < e) return !!set[lp.day] && lp.minute >= s && lp.minute < e;
    return (!!set[lp.day] && lp.minute >= s) || (!!set[yesterdayDay(lp)] && lp.minute < e);
  }

  function targets(screen, type, id) {
    if (type === 'tenant') return true;
    if (type === 'group') return !!screen.group_id && String(id) === String(screen.group_id);
    if (type === 'screen') return String(id) === String(screen.id);
    return false;
  }

  function activeOverride(screen, overrides, now) {
    var live = (overrides || []).filter(function (o) {
      if (o.cleared_at || !targets(screen, o.target_type, o.target_id)) return false;
      if (new Date(o.starts_at) > now) return false;
      if (o.ends_at && new Date(o.ends_at) <= now) return false;
      return true;
    });
    live.sort(function (a, b) {
      var r = (TARGET_RANK[b.target_type] || 0) - (TARGET_RANK[a.target_type] || 0);
      return r || (a.starts_at < b.starts_at ? 1 : a.starts_at > b.starts_at ? -1 : 0);
    });
    return live[0] || null;
  }

  function isOpen(screen, lp) {
    if (screen.always_on) return true;
    if ((screen.holiday_dates || []).indexOf(lp.iso) >= 0) return false;
    return inWindow(lp, screen.operating_days || [], screen.open_time || '06:00', screen.close_time || '18:00');
  }

  function resolve(screen, schedules, overrides, now) {
    now = now || new Date();
    if (screen.status === 'paused') return { mode: 'off' };
    var o = activeOverride(screen, overrides, now);
    if (o) return { mode: 'override', playlist_id: o.playlist_id, message: o.message, tone: o.tone, source_id: o.id };
    var lp = localParts(now, screen.timezone);
    if (!isOpen(screen, lp)) return { mode: 'after_hours' };
    var m = (schedules || []).filter(function (s) {
      return s.enabled !== false && targets(screen, s.target_type, s.target_id) &&
        (!s.start_date || lp.iso >= s.start_date) && (!s.end_date || lp.iso <= s.end_date) &&
        inWindow(lp, s.days, s.start_time, s.end_time);
    });
    if (m.length) {
      m.sort(function (a, b) {
        return (b.priority || 0) - (a.priority || 0) ||
          (TARGET_RANK[b.target_type] || 0) - (TARGET_RANK[a.target_type] || 0) ||
          (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0);
      });
      return { mode: 'schedule', playlist_id: m[0].playlist_id, source_id: m[0].id };
    }
    if (screen.default_playlist_id) return { mode: 'default', playlist_id: screen.default_playlist_id };
    if (screen.group_default_playlist_id) return { mode: 'default', playlist_id: screen.group_default_playlist_id };
    return { mode: 'legacy_pages' };
  }

  function itemLive(item, now) {
    if (item.enabled === false) return false;
    if (item.valid_from && new Date(item.valid_from) > now) return false;
    if (item.valid_to && new Date(item.valid_to) <= now) return false;
    return true;
  }

  var api = { resolve: resolve, itemLive: itemLive, inWindow: inWindow, localParts: localParts };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ChxResolver = api;
})(this);
