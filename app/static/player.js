/* GOFLIX custom player: fullscreen landscape, brightness/volume swipes, VLC fallback.
   Uses globals from watch.html: $, toast, FILENAME, ORIGINAL_SRC, BASE_URL_JS, SHORT_CODE */
(() => {
const v = $('player'), pb = $('pb'), seek = $('seek');
const IC = {
  play: '<svg viewBox="0 0 24 24"><path d="M7 4l13 8-13 8z"/></svg>',
  pause: '<svg viewBox="0 0 24 24"><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg>',
  vol: '<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 4V5L7 9zM16 8a5 5 0 010 8M18.5 5.5a9 9 0 010 13" stroke="#fff" stroke-width="2" fill="none"/></svg>',
  mute: '<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 4V5L7 9z"/><path d="M16 9l5 6M21 9l-5 6" stroke="#fff" stroke-width="2"/></svg>',
  fs: '<svg viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" stroke="#fff" stroke-width="2.2" fill="none"/></svg>',
};
const fmt = s => { s = Math.max(0, Math.floor(s || 0)); const h = s / 3600 | 0, m = s % 3600 / 60 | 0, x = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0'); };
const clamp = (n, a, b) => Math.min(b, Math.max(a, n));
const isFS = () => pb.classList.contains('fs');
const rotated = () => pb.classList.contains('rot');
$('pp').innerHTML = IC.play; $('big').innerHTML = IC.play; $('fsb').innerHTML = IC.fs; $('mute').innerHTML = IC.vol;

// ── play / pause / UI ────────────────────────────────────────────────────
let hideT;
const showCtl = () => { pb.classList.remove('hide'); clearTimeout(hideT);
  if (!v.paused) hideT = setTimeout(() => pb.classList.add('hide'), 3000); };
const toggleCtl = () => pb.classList.contains('hide') ? showCtl() : (v.paused ? 0 : pb.classList.add('hide'));
const toggle = () => v.paused ? v.play().catch(() => {}) : v.pause();
$('pp').onclick = $('big').onclick = e => { e.stopPropagation(); toggle(); showCtl(); };
v.addEventListener('play', () => { pb.classList.add('playing'); $('pp').innerHTML = IC.pause; showCtl(); });
v.addEventListener('pause', () => { pb.classList.remove('playing'); $('pp').innerHTML = IC.play; showCtl(); });

// ── progress line (played + buffered) ───────────────────────────────────
function paint() {
  const d = v.duration || 0, t = v.currentTime || 0, p = d ? t / d * 100 : 0;
  $('sfill').style.width = p + '%'; $('sthumb').style.left = p + '%';
  let b = 0; for (let i = 0; i < v.buffered.length; i++) if (v.buffered.start(i) <= t + 1) b = Math.max(b, v.buffered.end(i));
  $('sbuf').style.width = (d ? b / d * 100 : 0) + '%';
  $('tm').textContent = fmt(t) + ' / ' + fmt(d);
}
['timeupdate', 'progress', 'loadedmetadata', 'durationchange'].forEach(e => v.addEventListener(e, paint));
const ratio = e => { const r = seek.getBoundingClientRect();
  return clamp(rotated() ? (e.clientY - r.top) / r.height : (e.clientX - r.left) / r.width, 0, 1); };
let seeking = false;
seek.addEventListener('pointerdown', e => { seeking = true; seek.setPointerCapture(e.pointerId);
  if (v.duration) v.currentTime = ratio(e) * v.duration; paint(); showCtl(); });
seek.addEventListener('pointermove', e => { if (seeking && v.duration) { v.currentTime = ratio(e) * v.duration; paint(); } });
seek.addEventListener('pointerup', () => { seeking = false; });

// ── mute / volume / speed ───────────────────────────────────────────────
const syncVol = () => { $('mute').innerHTML = (v.muted || v.volume === 0) ? IC.mute : IC.vol; $('vol').value = v.muted ? 0 : v.volume; };
$('mute').onclick = () => { v.muted = !v.muted; syncVol(); };
$('vol').oninput = e => { v.muted = false; v.volume = +e.target.value; syncVol(); };
v.addEventListener('volumechange', syncVol); syncVol();
const SPEEDS = [1, 1.25, 1.5, 2, 0.75]; let si = 0;
$('spd').onclick = () => { si = (si + 1) % SPEEDS.length; v.playbackRate = SPEEDS[si]; $('spd').textContent = SPEEDS[si] + 'x'; };

// ── fullscreen: rotate to landscape ─────────────────────────────────────
function fixRot() { pb.classList.toggle('rot', isFS() && innerHeight > innerWidth); }
async function enterFS() {
  if (!pb.requestFullscreen && !pb.webkitRequestFullscreen && v.webkitEnterFullscreen) { v.webkitEnterFullscreen(); return; }
  pb.classList.add('fs');
  try { await (pb.requestFullscreen ? pb.requestFullscreen({ navigationUI: 'hide' }) : pb.webkitRequestFullscreen()); } catch (e) {}
  try { await screen.orientation.lock('landscape'); } catch (e) {}
  fixRot(); setTimeout(fixRot, 400); showCtl();
}
function exitFS() {
  pb.classList.remove('fs', 'rot');
  try { screen.orientation.unlock(); } catch (e) {}
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else if (document.webkitFullscreenElement) document.webkitExitFullscreen();
  setBright(1, true);
}
$('fsb').onclick = () => isFS() ? exitFS() : enterFS();
['fullscreenchange', 'webkitfullscreenchange'].forEach(e => document.addEventListener(e, () => {
  if (!document.fullscreenElement && !document.webkitFullscreenElement && isFS()) exitFS(); else fixRot(); }));
addEventListener('resize', fixRot); addEventListener('orientationchange', () => setTimeout(fixRot, 300));

// ── gestures: left swipe = brightness, right swipe = volume, double tap = ±10s ──
let bright = 1;
const hud = $('hud');
function showHud(icon, p) { hud.innerHTML = icon + '<u><i style="width:' + Math.round(p * 100) + '%"></i></u>';
  hud.classList.add('show'); clearTimeout(showHud.t); showHud.t = setTimeout(() => hud.classList.remove('show'), 900); }
function setBright(b, quiet) { bright = clamp(b, 0.2, 1.6); v.style.filter = bright === 1 ? '' : 'brightness(' + bright + ')';
  if (!quiet) showHud('☀', (bright - 0.2) / 1.4); }
function setVol(x) { v.muted = false; v.volume = clamp(x, 0, 1); showHud('🔊', v.volume); }
const loc = t => { const r = pb.getBoundingClientRect();
  return rotated() ? { x: t.clientY - r.top, y: r.right - t.clientX, w: r.height, h: r.width }
                   : { x: t.clientX - r.left, y: t.clientY - r.top, w: r.width, h: r.height }; };
function skip(sec, side) { v.currentTime = clamp(v.currentTime + sec, 0, v.duration || 1e9);
  const s = $('skip' + side); s.textContent = (sec > 0 ? '+' : '−') + '10s'; s.classList.add('show'); setTimeout(() => s.classList.remove('show'), 500); }
let g = null, lastTap = 0, tapT, lastTouch = 0;
pb.addEventListener('touchstart', e => { lastTouch = Date.now();
  if (e.target.closest('.ctl,.big')) return;
  g = { ...loc(e.touches[0]), mode: null, b: bright, vol: v.volume, moved: false }; }, { passive: true });
pb.addEventListener('touchmove', e => {
  if (!g) return; const p = loc(e.touches[0]), dy = g.y - p.y, dx = p.x - g.x;
  if (!g.mode && isFS()) {
    if (Math.abs(dy) > 12 && Math.abs(dy) > Math.abs(dx)) g.mode = g.x < g.w / 2 ? 'b' : 'v';
  }
  if (g.mode) { e.preventDefault(); g.moved = true; const d = dy / (g.h * 0.75);
    g.mode === 'b' ? setBright(g.b + d) : setVol(g.vol + d); }
  else if (Math.abs(dx) > 12 || Math.abs(dy) > 12) g.moved = true;
}, { passive: false });
pb.addEventListener('touchend', () => {
  if (!g) return; const s = g; g = null; if (s.moved) return;
  const third = s.w / 3, now = Date.now();
  if (now - lastTap < 300) { clearTimeout(tapT); lastTap = 0;
    if (s.x < third) skip(-10, 'L'); else if (s.x > 2 * third) skip(10, 'R'); else toggle(); }
  else { lastTap = now; tapT = setTimeout(toggleCtl, 300); }
});
pb.addEventListener('click', e => { if (Date.now() - lastTouch < 600 || e.target.closest('.ctl,.big')) return; toggle(); showCtl(); });
pb.addEventListener('dblclick', e => { if (!e.target.closest('.ctl')) $('fsb').click(); });
pb.addEventListener('mousemove', showCtl);
addEventListener('keydown', e => { if (/INPUT|TEXTAREA/.test(e.target.tagName)) return;
  if (e.code === 'Space') { e.preventDefault(); toggle(); } else if (e.key === 'f') $('fsb').click();
  else if (e.key === 'ArrowRight') skip(10, 'R'); else if (e.key === 'ArrowLeft') skip(-10, 'L'); else if (e.key === 'Escape' && isFS()) exitFS(); });

// ── buffering spinner ───────────────────────────────────────────────────
const sp = $('buffering');
['loadstart', 'waiting', 'seeking'].forEach(e => v.addEventListener(e, () => sp.classList.add('show')));
['playing', 'canplay', 'error', 'pause'].forEach(e => v.addEventListener(e, () => sp.classList.remove('show')));

// ── can't-play / no-audio → point to VLC ────────────────────────────────
const warn = $('warn');
function warnVlc(msg) { $('warn-msg').textContent = msg; warn.classList.add('show'); $('vlc-link').classList.add('glow'); }
function clearWarn() { warn.classList.remove('show'); $('vlc-link').classList.remove('glow'); }
v.addEventListener('error', () => warnVlc("This web player can't play this video format."));
function audioMissing() {
  if (v.muted || v.volume === 0 || v.paused || v.currentTime < 2) return null;
  if ('webkitAudioDecodedByteCount' in v) return v.webkitAudioDecodedByteCount === 0;
  if ('mozHasAudio' in v) return !v.mozHasAudio;
  if (v.audioTracks) return v.audioTracks.length === 0;
  return null;
}
v.addEventListener('playing', () => { clearWarn();
  [3000, 7000].forEach(ms => setTimeout(() => { const m = audioMissing();
    if (m === true) warnVlc("This web player can't play this file's audio (no sound). Tap VLC below, VLC can play it.");
    else if (m === false) clearWarn(); }, ms)); });

// ── resume playback ─────────────────────────────────────────────────────
const RK = 'goflix_resume_' + SHORT_CODE; let lastSave = 0;
const getSaved = () => { try { return parseFloat(localStorage.getItem(RK)) || 0; } catch (e) { return 0; } };
v.addEventListener('loadedmetadata', () => { const s = getSaved();
  if (s > 5 && v.duration && s < v.duration - 5) { v.currentTime = s; toast('Resumed from ' + fmt(s)); } }, { once: true });
v.addEventListener('timeupdate', () => { if (v.currentTime - lastSave >= 5) { lastSave = v.currentTime; try { localStorage.setItem(RK, String(Math.floor(lastSave))); } catch (e) {} } });
v.addEventListener('ended', () => { try { localStorage.removeItem(RK); } catch (e) {} });

// ── Stream in Apps (proper Android intents / iOS schemes) ───────────────
const UA = navigator.userAgent, ANDROID = /Android/i.test(UA), IOS = /iP(hone|ad|od)/.test(UA);
const abs = () => (/^https?:\/\//.test(BASE_URL_JS) ? BASE_URL_JS : 'https://' + BASE_URL_JS) + ORIGINAL_SRC;
function intent(pkg) { const u = new URL(abs());
  return 'intent://' + u.host + u.pathname + '#Intent;scheme=' + u.protocol.slice(0, -1) +
    ';action=android.intent.action.VIEW;type=video/*;package=' + pkg + ';S.title=' + encodeURIComponent(FILENAME) + ';end'; }
const APPS = {
  'vlc-link': { pkg: 'org.videolan.vlc', ios: () => 'vlc-x-callback://x-callback-url/stream?url=' + encodeURIComponent(abs()) },
  'mx-link': { pkg: 'com.mxtech.videoplayer.ad' },
  'nplayer-link': { pkg: 'com.newin.nplayer.pro', ios: () => abs().replace(/^http/, 'nplayer-http') },
  'playit-link': { pkg: 'com.playit.videoplayer' },
};
Object.entries(APPS).forEach(([id, a]) => $(id).addEventListener('click', e => { e.preventDefault(); v.pause();
  if (ANDROID) location.href = intent(a.pkg);
  else if (IOS && a.ios) location.href = a.ios();
  else { copyLink(true); toast('Link copied. Paste it in ' + $(id).textContent + ' (Ctrl+N in VLC)'); } }));
paint();
})();
