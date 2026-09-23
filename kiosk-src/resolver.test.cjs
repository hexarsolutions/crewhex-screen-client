// node web/public/player/resolver.test.cjs  — same cases as api/tests/test_signage_resolver.py
const assert = require('assert');
const { resolve, inWindow, localParts } = require('./resolver.js');
const at = (y, mo, d, h, mi = 0) => new Date(Date.UTC(y, mo - 1, d, h - 10, mi)); // Brisbane wall clock
const MON9 = at(2026, 9, 21, 9);
const scr = (kw = {}) => Object.assign({ id: 's1', group_id: 'g1', status: 'online', timezone: 'Australia/Brisbane',
  operating_days: [1, 2, 3, 4, 5], open_time: '06:00', close_time: '18:00', holiday_dates: [] }, kw);
const sch = (id, pl, kw = {}) => Object.assign({ id, playlist_id: pl, target_type: 'tenant', target_id: null,
  days: [1, 2, 3, 4, 5], start_time: '07:00', end_time: '17:00', priority: 0, created_at: '2026-01-01', enabled: true }, kw);
const ov = (id, tt, tid, pl, msg, start, end) => ({ id, target_type: tt, target_id: tid, playlist_id: pl, message: msg, starts_at: start, ends_at: end });
let n = 0; const t = (name, fn) => { fn(); n++; };

t('paused', () => assert.equal(resolve(scr({ status: 'paused' }), [], [ov('o', 'tenant', null, 'P', null, '2026-01-01T00:00:00Z')], MON9).mode, 'off'));
t('override after hours', () => { const r = resolve(scr(), [], [ov('o', 'tenant', null, null, 'Evacuate', '2026-01-01T00:00:00Z')], at(2026, 9, 21, 23)); assert.equal(r.mode, 'override'); assert.equal(r.message, 'Evacuate'); });
t('override expiry', () => assert.equal(resolve(scr(), [], [ov('o', 'tenant', null, 'P', null, '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z'), ov('f', 'tenant', null, 'P', null, '2027-01-01T00:00:00Z')], MON9).mode, 'legacy_pages'));
t('screen override wins', () => assert.equal(resolve(scr(), [], [ov('t', 'tenant', null, 'T', null, '2026-01-01T00:00:00Z'), ov('s', 'screen', 's1', 'S', null, '2025-01-01T00:00:00Z')], MON9).playlist_id, 'S'));
t('after hours', () => {
  assert.equal(resolve(scr(), [sch('a', 'P')], [], at(2026, 9, 21, 19)).mode, 'after_hours');
  assert.equal(resolve(scr(), [sch('a', 'P')], [], at(2026, 9, 26, 10)).mode, 'after_hours');
  assert.equal(resolve(scr({ holiday_dates: ['2026-09-21'] }), [sch('a', 'P')], [], MON9).mode, 'after_hours');
  assert.equal(resolve(scr({ always_on: true }), [sch('a', 'P', { days: [0,1,2,3,4,5,6], start_time: '00:00', end_time: '00:00' })], [], at(2026, 9, 26, 3)).mode, 'schedule');
});
t('priority/specificity/newest', () => {
  const s = [sch('t', 'TENANT'), sch('g', 'GROUP', { target_type: 'group', target_id: 'g1' }), sch('x', 'SCREEN', { target_type: 'screen', target_id: 's1' })];
  assert.equal(resolve(scr(), s, [], MON9).playlist_id, 'SCREEN');
  s.push(sch('hi', 'HIGH', { priority: 5 }));
  assert.equal(resolve(scr(), s, [], MON9).playlist_id, 'HIGH');
  assert.equal(resolve(scr(), [sch('old', 'OLD'), sch('new', 'NEW', { created_at: '2026-06-01' })], [], MON9).playlist_id, 'NEW');
});
t('other targets ignored', () => assert.equal(resolve(scr(), [sch('g', 'O', { target_type: 'group', target_id: 'g2' }), sch('x', 'O2', { target_type: 'screen', target_id: 's9' })], [], MON9).mode, 'legacy_pages'));
t('date range', () => {
  const s = [sch('d', 'XMAS', { start_date: '2026-12-01', end_date: '2026-12-24' })];
  assert.equal(resolve(scr(), s, [], MON9).mode, 'legacy_pages');
  assert.equal(resolve(scr(), s, [], at(2026, 12, 1, 9)).playlist_id, 'XMAS');
});
t('defaults', () => {
  assert.equal(resolve(scr({ default_playlist_id: 'D', group_default_playlist_id: 'G' }), [], [], MON9).playlist_id, 'D');
  assert.equal(resolve(scr({ group_default_playlist_id: 'G' }), [], [], MON9).playlist_id, 'G');
});
t('overnight', () => {
  const lp = d => localParts(d, 'UTC');
  assert.ok(inWindow(lp(new Date(Date.UTC(2026, 8, 25, 23))), [5], '22:00', '06:00'));
  assert.ok(inWindow(lp(new Date(Date.UTC(2026, 8, 26, 3))), [5], '22:00', '06:00'));
  assert.ok(!inWindow(lp(new Date(Date.UTC(2026, 8, 26, 23))), [5], '22:00', '06:00'));
});
t('timezone', () => {
  assert.equal(resolve(scr({ timezone: 'Australia/Perth', open_time: '07:00' }), [], [], at(2026, 9, 21, 8, 30)).mode, 'after_hours');
  assert.equal(resolve(scr({ open_time: '07:00' }), [], [], at(2026, 9, 21, 8, 30)).mode, 'legacy_pages');
});
console.log(`resolver.js: ${n} groups passed`);
