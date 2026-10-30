'use strict';

// The reading layout and the dividers between the library, the claims, and
// the paper. Shares the library state declared in app.js.

// The paper whose PDF is shown beside the claims: the one selected, when it
// has a PDF and the reader has not put it away.
function readingPaper() {
  if (V.view !== 'claims' || !V.paper || V.pdfHidden) return null;
  const paper = S.papers.find((p) => p.key === V.paper);
  return paper && paper.has_pdf ? V.paper : null;
}

// Reading has its own layout. Library filters remain available on demand and
// keep their state when the user returns to the library.
let readingClaimsCollapsed = false;
function layoutPanes(reading) {
  const wasReading = document.body.classList.contains('reading-paper');
  document.body.classList.toggle('reading-paper', Boolean(reading));
  $('panes').classList.toggle('reading', Boolean(reading));
  if (Boolean(reading) !== wasReading) {
    $('main').scrollTop = 0;
    if (reading) $('paper-import').open = false;
    document.body.classList.remove('library-collapsed', 'reading-filters-open');
    const narrow = reading && window.matchMedia('(max-width: 800px)').matches;
    setReadingLibrary(Boolean(narrow));
  }
  document.body.classList.toggle('claims-collapsed', Boolean(reading) && readingClaimsCollapsed);
  fitClaimsWidth();
  const filtersOpen = document.body.classList.contains('reading-filters-open');
  const filtered = Boolean(V.q || V.kind || V.tag || V.label || V.facet || V.unreviewed || V.unverified);
  $('btn-reader-filters').textContent = filtered ? 'Claim filters · active' : 'Claim filters';
  $('btn-reader-filters').setAttribute('aria-expanded', String(filtersOpen));
  ResearchTools.mountReader(reading);
}

function setReadingLibrary(collapsed) {
  if (collapsed && $('side').contains(document.activeElement)) $('btn-reader-library').focus();
  document.body.classList.toggle('library-collapsed', collapsed);
  $('btn-reader-library').textContent = collapsed ? 'Show library' : 'Hide library';
  $('btn-reader-library').setAttribute('aria-expanded', String(!collapsed));
  fitClaimsWidth();
}

function setReadingClaims(collapsed) {
  if (collapsed && $('claim-column').contains(document.activeElement)) $('btn-reader-claims').focus();
  readingClaimsCollapsed = collapsed;
  document.body.classList.toggle('claims-collapsed', document.body.classList.contains('reading-paper') && collapsed);
  $('btn-reader-claims').textContent = collapsed ? 'Show claims' : 'Hide claims';
  $('btn-reader-claims').setAttribute('aria-expanded', String(!collapsed));
  fitClaimsWidth();
}

function setReadingFilters(open) {
  document.body.classList.toggle('reading-filters-open', open);
  $('btn-reader-filters').setAttribute('aria-expanded', String(open));
}

// --- the width of the side pane -------------------------------------------
//
// How the window is divided between the lists and the claims is the reader's
// to set: drag the divider, or give it focus and use the arrow keys. A
// double-click puts it back where it started. Remembered in this browser like
// the other view preferences, since it is about the screen being read on.

const SIDE_WIDTH_KEY = 'doxograph-side-width';
const SIDE_WIDTH_MIN = 180;
// The 20rem the pane has always been, measured rather than written as 320px: a
// browser set to a larger default font size draws the lists larger too, and the
// width it starts at has to grow with them.
const sideWidthDefault = () =>
  20 * (parseFloat(getComputedStyle(document.documentElement).fontSize) || 16);
// Half the window is as far as the divider goes. Past that the side pane has
// stopped being a side pane and there is nothing left to read beside it. The
// floor gives way to that ceiling on a window too narrow to hold both.
const sideWidthMax = () => Math.round(window.innerWidth / 2);
const sideWidthMin = () => Math.min(SIDE_WIDTH_MIN, sideWidthMax());
const clampSideWidth = (px) => Math.min(sideWidthMax(), Math.max(sideWidthMin(), Math.round(px)));

function readSideWidth() {
  try {
    const saved = Number(localStorage.getItem(SIDE_WIDTH_KEY));
    return Number.isFinite(saved) && saved > 0 ? saved : sideWidthDefault();
  } catch (error) {
    return sideWidthDefault();
  }
}

// The width that was asked for, which is not always the width on screen: a
// window too narrow to hold it shows the widest pane it can. Kept here as well
// as in storage, so a browser that refuses to write still holds the choice for
// the rest of the session, and a window narrowed and widened again comes back
// to it rather than to the default.
let sideWidthWanted = readSideWidth();

// A drag or a key can ask past the ceiling; what is remembered is the most this
// window would have given it, not where the pointer went, so a drag that ran off
// the edge does not spring the pane open on a wider screen later.
function applySideWidth(px, persist = false) {
  return wantSideWidth(clampSideWidth(px), persist);
}

// The reset is the exception: Home and a double-click mean 20rem, whatever this
// window can show of it, so that is what is remembered and a wider window comes
// back to it.
function wantSideWidth(px, persist = false) {
  sideWidthWanted = Math.round(px);
  if (persist) storeSideWidth();
  return fitSideWidth();
}

// Put the pane at the width that was asked for, or as near as this window
// allows, without forgetting what was asked for.
function fitSideWidth() {
  const width = clampSideWidth(sideWidthWanted);
  $('layout').style.setProperty('--side-width', `${width}px`);
  const handle = $('side-resize');
  handle.setAttribute('aria-valuenow', String(width));
  handle.setAttribute('aria-valuemin', String(sideWidthMin()));
  handle.setAttribute('aria-valuemax', String(sideWidthMax()));
  return width;
}

function storeSideWidth() {
  try { localStorage.setItem(SIDE_WIDTH_KEY, String(sideWidthWanted)); }
  catch (error) { /* the pane keeps the width for this session */ }
}

const sideWidthNow = () => $('side').getBoundingClientRect().width;

fitSideWidth();

window.addEventListener('resize', fitSideWidth);

{
  const handle = $('side-resize');
  // The width the drag started from, not the pointer's distance from the
  // divider: the divider is 8px wide, so the two are not the same, and taking
  // the difference keeps the pane from jumping on the first move.
  let drag = null;
  // Two quick nudges of the divider inside the double-click interval make the
  // browser synthesize a dblclick out of them. Only a pair of presses that both
  // stayed still is a reset; two small drags are two small drags.
  let movedThisPress = false;
  let movedLastPress = false;
  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    // Cancelling the press keeps the browser from selecting text under the
    // drag, but it also stops the divider taking focus, which would leave the
    // arrow keys on whatever was focused before. Give it focus by hand.
    event.preventDefault();
    handle.focus();
    movedLastPress = movedThisPress;
    movedThisPress = false;
    drag = { pointer: event.pointerId, from: event.clientX, width: sideWidthNow(), wanted: sideWidthWanted };
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add('resizing-side');
  });
  handle.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.pointer) return;
    // A couple of pixels is a hand resting on the button, not a drag.
    if (Math.abs(event.clientX - drag.from) > 2) movedThisPress = true;
    applySideWidth(drag.width + event.clientX - drag.from);
  });
  const release = (event) => {
    if (!drag || event.pointerId !== drag.pointer) return;
    // A drag that left the divider where it found it is not a new width — it
    // was pushed into the floor or the ceiling, or brought back. Put back what
    // was asked for before it, so a pane this window is only showing part of
    // survives being pushed at.
    if (clampSideWidth(sideWidthWanted) === clampSideWidth(drag.wanted)) sideWidthWanted = drag.wanted;
    drag = null;
    document.body.classList.remove('resizing-side');
    storeSideWidth();   // write once, at the end of the drag
  };
  handle.addEventListener('pointerup', release);
  handle.addEventListener('pointercancel', release);
  handle.addEventListener('lostpointercapture', release);
  handle.addEventListener('dblclick', () => {
    if (!movedThisPress && !movedLastPress) wantSideWidth(sideWidthDefault(), true);
  });
  // A key that cannot move the divider — the pane is already against the floor
  // or the ceiling — leaves the remembered width where it is. Otherwise a press
  // that does nothing on a narrow window would quietly give up a wider pane the
  // window will be able to show again.
  const nudge = (px) => {
    if (clampSideWidth(px) !== clampSideWidth(sideWidthWanted)) applySideWidth(px, true);
  };
  handle.addEventListener('keydown', (event) => {
    const step = event.shiftKey ? 48 : 12;
    const at = clampSideWidth(sideWidthWanted);
    if (event.key === 'ArrowLeft') nudge(at - step);
    else if (event.key === 'ArrowRight') nudge(at + step);
    else if (event.key === 'Home') wantSideWidth(sideWidthDefault(), true);
    else return;
    event.preventDefault();
  });
}

// The claims column keeps its own width. ResizeObserver also refits it when
// the library opens or changes width, without replacing the mounted reader.
const CLAIMS_WIDTH_KEY = 'doxograph-claims-width';
let claimsWidthWanted = null;
try {
  const saved = Number(localStorage.getItem(CLAIMS_WIDTH_KEY));
  if (Number.isFinite(saved) && saved > 0) claimsWidthWanted = saved;
} catch (_) { /* use the responsive default */ }
const claimsWidthDefault = () => Math.max(
  (window.innerWidth <= 1100 ? 17 : 19) * (parseFloat(getComputedStyle(document.documentElement).fontSize) || 16),
  $('main').clientWidth * (window.innerWidth <= 1100 ? .34 : .3));
const claimsWidthMax = () => Math.max(0, $('main').clientWidth - 280);
const claimsWidthMin = () => Math.min(240, claimsWidthMax());
const clampClaimsWidth = px => Math.min(claimsWidthMax(), Math.max(claimsWidthMin(), Math.round(px)));

function fitClaimsWidth() {
  if (!document.body.classList.contains('reading-paper') || window.innerWidth <= 800) return;
  const width = clampClaimsWidth(claimsWidthWanted ?? claimsWidthDefault());
  $('panes').style.setProperty('--claims-width', `${width}px`);
  const handle = $('claims-resize');
  handle.setAttribute('aria-valuenow', String(width));
  handle.setAttribute('aria-valuemin', String(claimsWidthMin()));
  handle.setAttribute('aria-valuemax', String(claimsWidthMax()));
}
function storeClaimsWidth() {
  try {
    if (claimsWidthWanted === null) localStorage.removeItem(CLAIMS_WIDTH_KEY);
    else localStorage.setItem(CLAIMS_WIDTH_KEY, String(claimsWidthWanted));
  } catch (_) { /* retain the width for this session */ }
}
new ResizeObserver(fitClaimsWidth).observe($('main'));
window.addEventListener('resize', fitClaimsWidth);

{
  const handle = $('claims-resize');
  let drag = null, movedThisPress = false, movedLastPress = false;
  handle.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault();
    handle.focus();
    movedLastPress = movedThisPress;
    movedThisPress = false;
    drag = { pointer: event.pointerId, from: event.clientX,
      width: $('claim-column').getBoundingClientRect().width, wanted: claimsWidthWanted };
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add('resizing-claims');
  });
  handle.addEventListener('pointermove', event => {
    if (!drag || event.pointerId !== drag.pointer) return;
    if (Math.abs(event.clientX - drag.from) > 2) movedThisPress = true;
    // Moving the right divider left makes the claims column wider.
    claimsWidthWanted = clampClaimsWidth(drag.width + drag.from - event.clientX);
    fitClaimsWidth();
  });
  const release = event => {
    if (!drag || event.pointerId !== drag.pointer) return;
    if (clampClaimsWidth(claimsWidthWanted ?? claimsWidthDefault()) === Math.round(drag.width)) {
      claimsWidthWanted = drag.wanted;
    }
    drag = null;
    document.body.classList.remove('resizing-claims');
    storeClaimsWidth();
  };
  handle.addEventListener('pointerup', release);
  handle.addEventListener('pointercancel', release);
  handle.addEventListener('lostpointercapture', release);
  const reset = () => { claimsWidthWanted = null; storeClaimsWidth(); fitClaimsWidth(); };
  handle.addEventListener('dblclick', () => {
    if (!movedThisPress && !movedLastPress) reset();
  });
  handle.addEventListener('keydown', event => {
    const at = clampClaimsWidth(claimsWidthWanted ?? claimsWidthDefault());
    const step = event.shiftKey ? 48 : 12;
    if (event.key === 'Home') reset();
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      const width = clampClaimsWidth(at + (event.key === 'ArrowLeft' ? step : -step));
      if (width !== at) { claimsWidthWanted = width; storeClaimsWidth(); fitClaimsWidth(); }
    } else return;
    event.preventDefault();
  });
}

$('btn-reader-library').addEventListener('click', () => {
  setReadingLibrary(!document.body.classList.contains('library-collapsed'));
});
$('btn-reader-claims').addEventListener('click', () => {
  setReadingClaims(!readingClaimsCollapsed);
});
window.matchMedia('(max-width: 800px)').addEventListener('change', event => {
  if (readingPaper()) setReadingLibrary(event.matches);
});
$('btn-reader-filters').addEventListener('click', () => {
  setReadingFilters(!document.body.classList.contains('reading-filters-open'));
});
$('btn-back-library').addEventListener('click', () => {
  // Use the existing navigation path so every editor parks its draft.
  $('papers').querySelector('[data-paper=""]').click();
  $('q').focus({preventScroll: true});
});
