'use strict';

let mapClient;
function connectPaperMap(client) { mapClient = client; }

// --- the map: papers as nodes ----------------------------------------------

// A force layout on a canvas, with no library behind it. Nodes are papers;
// squares are the ledger's own claims. Three kinds of edge: two papers whose
// claims share a topic, a tension between two papers, and a paper bearing on
// one of my claims. Everything is computed here from the client snapshot; the server knows
// nothing about the map.
//
// Layout state lives outside `V`. Positions are kept across redraws so the
// poll noticing a change does not reshuffle the map, but they are not a view
// choice, and they are dropped with the rest of a workspace's state.
const GRAPH = {
  nodes: new Map(),        // id -> node
  edges: [],
  alpha: 0,                // simulation temperature; 0 means at rest
  frame: 0,                // requestAnimationFrame handle, 0 when stopped
  hover: null, drag: null, pan: null, moved: false,
  zoom: 1, tx: 0, ty: 0,   // world -> screen: centre + (world * zoom) + (tx, ty)
  autofit: true,           // keep everything in view until the user takes over
  canvas: null, ctx: null, observer: null, signature: '', maxShared: 1, minShared: 1,
};

function graphReset() {
  graphStop();
  GRAPH.nodes.clear();
  GRAPH.edges = [];
  GRAPH.hover = GRAPH.drag = GRAPH.pan = null;
  GRAPH.zoom = 1; GRAPH.tx = GRAPH.ty = 0; GRAPH.autofit = true;
  GRAPH.canvas = GRAPH.ctx = null;
  GRAPH.signature = '';
  if (GRAPH.observer) { GRAPH.observer.disconnect(); GRAPH.observer = null; }
}

function graphStop() {
  if (GRAPH.frame) cancelAnimationFrame(GRAPH.frame);
  GRAPH.frame = 0;
  GRAPH.alpha = 0;
  if (GRAPH.observer) { GRAPH.observer.disconnect(); GRAPH.observer = null; }
  GRAPH.canvas = GRAPH.ctx = null;
}

function renderGraphNav() {
  const papers = mapClient.state.papers.length;
  $('graph-nav').innerHTML = `<li class="${V.view === 'graph' ? 'active' : ''}" data-view="graph">
    <span class="pt">How the papers connect</span>
    <span class="pm">${papers ? `${papers} papers as a map` : 'nothing to map yet'}</span></li>`;
}

// Which papers cite which, read off the PDFs by the server. Asked for when the
// map opens rather than with the rest of the state: nothing else wants it, and
// answering means reading every paper's reference list. The map draws without
// them until the answer lands.
let CITATIONS = [];
let citationsSeq = 0;
let citationsFailed = false;
const citationsInFlight = new Map();   // workspace -> the asking out for it
const citationsLatest = new Map();    // workspace -> the newest asking made
let citationsStale = false;           // the corpus moved while one was out

async function loadCitations() {
  // Whose citations these are, and which asking. A request left in flight when
  // the workspace changes answers with the other corpus's edges, and its paper
  // keys can collide with this one's; two askings in the same workspace can
  // also come back in the other order, the older last. Either way the answer
  // that is not the current question is dropped.
  // One asking per corpus at a time. Reading a corpus takes as long as it
  // takes and the poll comes round every two and a half seconds, so without
  // this a retry after a failure would start another scan on every tick until
  // one finished. A different corpus is a different question, and asks.
  const workspace = mapClient.workspaceId;
  if (citationsInFlight.has(workspace)) {
    // The corpus moved while this one was reading it, so its answer describes
    // papers that have changed since. Asked again once it is out of the way.
    citationsStale = true;
    return;
  }
  const seq = ++citationsSeq;
  // Kept per corpus and stamped with this asking: going A → B → A starts a
  // second reading for A, and the first to come back must not clear the
  // guard the second is relying on — nor be thrown away for being older than
  // a reading of somebody else's corpus, which a single counter would do.
  citationsInFlight.set(workspace, seq);
  citationsLatest.set(workspace, seq);
  citationsStale = false;
  let edges;
  try {
    const found = await mapClient.request('/api/citations');
    edges = found.edges || [];
  } catch (error) {
    // The server may be restarting. Leave the arrows that are drawn where
    // they are and ask again on the next poll: the corpus need never change
    // again, so waiting for it to would leave the map bare for the session.
    citationsFailed = true;
    return;
  } finally {
    if (citationsInFlight.get(workspace) === seq) citationsInFlight.delete(workspace);
  }
  if (seq !== citationsLatest.get(workspace) || workspace !== mapClient.workspaceId) return;
  citationsFailed = false;
  CITATIONS = edges;
  if (V.view === 'graph') renderGraph();
  // These describe the corpus as it was when the reading started. Drawn
  // anyway — they are closer to the truth than what was there — and then
  // asked for again, now that the way is clear.
  if (citationsStale) {
    citationsStale = false;
    loadCitations();
  }
}

function graphCite(p) {
  const who = (p.authors || [])[0] ? p.authors[0].split(' ').pop() : p.key;
  return `${who} ${p.year || ''}`.trim();
}

// The nodes and edges the current options call for, computed fresh from the client snapshot.
// `maxShared` is the heaviest topic edge before the threshold is applied, so
// the slider's range can follow the corpus.
function graphData() {
  const opts = V.graph;
  const byPaper = new Map(mapClient.state.papers.map((p) => [p.key, p]));
  const tagCount = new Map();     // paper -> Map(tag -> claims with it)
  const tagPapers = new Map();    // tag -> Set(paper)
  for (const c of mapClient.state.claims) {
    if (!byPaper.has(c.paper)) continue;
    for (const t of c.tags || []) {
      if (!tagCount.has(c.paper)) tagCount.set(c.paper, new Map());
      const counts = tagCount.get(c.paper);
      counts.set(t, (counts.get(t) || 0) + 1);
      if (!tagPapers.has(t)) tagPapers.set(t, new Set());
      tagPapers.get(t).add(c.paper);
    }
  }
  const nodes = [];
  for (const p of mapClient.state.papers) {
    const counts = tagCount.get(p.key) || new Map();
    const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    nodes.push({
      id: `p:${p.key}`, type: 'paper', key: p.key, label: p.title || p.key, cite: graphCite(p),
      n: p.n_claims || 0, topic: ranked.length ? ranked[0][0] : null, tags: new Set(counts.keys()),
      labels: new Set(p.labels || []),
      r: 6 + 3 * Math.sqrt(p.n_claims || 0),
    });
  }
  const edges = [];
  let maxShared = 1;
  let minShared = opts.minShared || 1;
  // The weights are worked out whether or not topic edges are drawn, so the
  // slider keeps its range while the layer is off and the threshold survives
  // turning it back on.
  {
    const pairs = new Map();
    for (const [tag, set] of tagPapers) {
      const list = [...set].sort();
      for (let i = 0; i < list.length; i += 1) {
        for (let j = i + 1; j < list.length; j += 1) {
          const key = `${list[i]}|${list[j]}`;
          if (!pairs.has(key)) pairs.set(key, { type: 'topic', a: `p:${list[i]}`, b: `p:${list[j]}`, w: 0, tags: [] });
          const pair = pairs.get(key);
          pair.w += Math.min(tagCount.get(list[i]).get(tag), tagCount.get(list[j]).get(tag));
          pair.tags.push(tag);
        }
      }
    }
    // With no threshold chosen, the median weight: a corpus on one subject
    // shares something between nearly every pair, and drawing all of it hides
    // the structure. The slider shows the value picked.
    const weights = [...pairs.values()].map((e) => e.w).sort((a, b) => a - b);
    maxShared = weights.length ? Math.max(1, weights[weights.length - 1]) : 1;
    minShared = opts.minShared || (weights.length ? weights[Math.floor(weights.length / 2)] : 1);
    // A threshold chosen for a bigger corpus can exceed every weight here,
    // which would hide every topic link while the label promised otherwise.
    // The clamp is written back to the choice, so what the slider shows is
    // what later recomputations start from, rather than a value it once had.
    minShared = Math.min(minShared, maxShared);
    if (opts.minShared && opts.minShared > maxShared) opts.minShared = maxShared;
    if (opts.topics) for (const pair of pairs.values()) if (pair.w >= minShared) edges.push(pair);
  }
  if (opts.tensions) {
    const pairs = new Map();
    for (const t of mapClient.state.tensions || []) {
      if (t.status === 'dismissed' || !t.claims || t.claims.length !== 2) continue;
      const [a, b] = [t.claims[0].paper, t.claims[1].paper].sort();
      if (a === b || !byPaper.has(a) || !byPaper.has(b)) continue;
      const key = `${a}|${b}`;
      if (!pairs.has(key)) pairs.set(key, { type: 'tension', a: `p:${a}`, b: `p:${b}`, n: 0, open: 0, kinds: new Set() });
      const pair = pairs.get(key);
      pair.n += 1;
      if (t.status === 'open') pair.open += 1;
      pair.kinds.add(t.kind);
    }
    // A tension is found within a shared topic, so the topic edge between the
    // same two papers says nothing the tension does not, and drawn together
    // the solid topic stroke would show through the dashes of an open one.
    // Dropped here rather than at draw time, so the link count agrees with
    // what is on screen.
    for (let i = edges.length - 1; i >= 0; i -= 1) {
      if (edges[i].type === 'topic' && pairs.has(`${edges[i].a.slice(2)}|${edges[i].b.slice(2)}`)) edges.splice(i, 1);
    }
    edges.push(...pairs.values());
  }
  if (opts.cites) {
    const drawn = new Set();
    for (const edge of CITATIONS) {
      if (!byPaper.has(edge.from) || !byPaper.has(edge.to) || edge.from === edge.to) continue;
      const key = `${edge.from}|${edge.to}`;
      if (drawn.has(key)) continue;
      drawn.add(key);
      edges.push({ type: 'cite', a: `p:${edge.from}`, b: `p:${edge.to}` });
    }
    // One paper citing another says more than the two sharing a tag, and the
    // topic stroke under the arrow would only thicken it. Dropped as a tension
    // drops one, in either direction, so the link count matches the screen.
    for (let i = edges.length - 1; i >= 0; i -= 1) {
      if (edges[i].type !== 'topic') continue;
      const [a, b] = [edges[i].a.slice(2), edges[i].b.slice(2)];
      if (drawn.has(`${a}|${b}`) || drawn.has(`${b}|${a}`)) edges.splice(i, 1);
    }
  }
  if (opts.ledger) {
    const own = new Map((mapClient.state.ledger || []).map((c) => [c.id, c]));
    const pairs = new Map();
    const used = new Set();
    for (const c of mapClient.state.claims) {
      if (!byPaper.has(c.paper)) continue;
      for (const link of c.ledger_links || []) {
        if (!own.has(link.claim)) continue;
        // `independent` records that a paper does not bear on the claim; a
        // line would say the opposite, so it is left off the map.
        if (link.relation === 'independent') continue;
        used.add(link.claim);
        const key = `${c.paper}|${link.claim}|${link.relation}`;
        if (!pairs.has(key)) pairs.set(key, { type: 'ledger', a: `p:${c.paper}`, b: `l:${link.claim}`, relation: link.relation, n: 0 });
        pairs.get(key).n += 1;
      }
    }
    for (const id of [...used].sort()) {
      nodes.push({ id: `l:${id}`, type: 'claim', key: id, label: own.get(id).text || id, cite: id, n: 0, r: 7, tags: new Set() });
    }
    edges.push(...pairs.values());
  }
  return { nodes, edges, maxShared, minShared };
}

// Merge fresh data into the layout: nodes that were already placed keep their
// position, a new node lands beside a neighbour it is joined to or on a ring
// around the middle, and nodes that are gone are dropped. Returns whether the
// set of nodes or edges changed, which is what warrants reheating.
function graphSync() {
  const { nodes, edges, maxShared, minShared } = graphData();
  const signature = JSON.stringify([nodes.map((n) => [n.id, n.n, n.topic]),
    edges.map((e) => [e.type, e.a, e.b, e.w, e.n, e.open, e.relation])]);
  const changed = signature !== GRAPH.signature;
  GRAPH.signature = signature;
  const keep = new Set(nodes.map((n) => n.id));
  for (const id of [...GRAPH.nodes.keys()]) if (!keep.has(id)) GRAPH.nodes.delete(id);
  const spread = 40 + 22 * Math.sqrt(nodes.length);
  nodes.forEach((fresh, i) => {
    const had = GRAPH.nodes.get(fresh.id);
    if (had) { Object.assign(had, fresh); return; }
    const near = edges.map((e) => (e.a === fresh.id ? e.b : e.b === fresh.id ? e.a : null))
      .map((id) => id && GRAPH.nodes.get(id)).find((n) => n && Number.isFinite(n.x));
    const angle = (i * 2.399963) % (2 * Math.PI);   // golden angle: spaced, not clumped
    const x = near ? near.x + 30 * Math.cos(angle) : spread * Math.cos(angle) * (0.4 + 0.6 * Math.random());
    const y = near ? near.y + 30 * Math.sin(angle) : spread * Math.sin(angle) * (0.4 + 0.6 * Math.random());
    GRAPH.nodes.set(fresh.id, { ...fresh, x, y, vx: 0, vy: 0, fixed: false });
  });
  GRAPH.edges = edges.map((e) => ({ ...e, source: GRAPH.nodes.get(e.a), target: GRAPH.nodes.get(e.b) }))
    .filter((e) => e.source && e.target);
  GRAPH.maxShared = maxShared;
  GRAPH.minShared = minShared;
  if (GRAPH.hover && !GRAPH.nodes.has(GRAPH.hover.id)) GRAPH.hover = null;
  return changed;
}

// One step of the simulation: pairwise repulsion, springs along edges, a pull
// to the middle, then damped integration. Forces scale with `alpha`, which
// cools each step; the loop stops once it is near zero.
function graphTick() {
  const nodes = [...GRAPH.nodes.values()];
  const alpha = GRAPH.alpha;
  for (let i = 0; i < nodes.length; i += 1) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j += 1) {
      const b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = (Math.random() - 0.5); dy = (Math.random() - 0.5); d2 = 1; }
      if (d2 > 250000) continue;   // beyond 500 units nothing pushes, so a loner is not flung off
      const f = (900 * alpha) / d2;
      a.vx -= dx * f; a.vy -= dy * f;
      b.vx += dx * f; b.vy += dy * f;
    }
  }
  for (const e of GRAPH.edges) {
    const { source: a, target: b } = e;
    const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
    const rest = e.type === 'topic' ? Math.max(70, 150 - 10 * Math.min(e.w, 8)) : e.type === 'tension' ? 110 : 90;
    const k = e.type === 'topic' ? 0.04 : 0.08;
    const f = ((d - rest) / d) * k * alpha;
    a.vx += dx * f; a.vy += dy * f;
    b.vx -= dx * f; b.vy -= dy * f;
  }
  for (const n of nodes) {
    // My own claims hang off one or two papers each; a stronger pull keeps
    // them among the papers rather than orbiting the whole map.
    const pull = n.type === 'claim' ? 0.05 : 0.015;
    n.vx -= n.x * pull * alpha;
    n.vy -= n.y * pull * alpha;
    n.vx *= 0.6; n.vy *= 0.6;
    if (!n.fixed) { n.x += n.vx; n.y += n.vy; }
  }
  GRAPH.alpha = alpha * 0.975;
  if (GRAPH.alpha < 0.003) GRAPH.alpha = 0;
}

function graphColors() {
  const style = getComputedStyle(document.documentElement);
  const read = (name) => style.getPropertyValue(`--${name}`).trim();
  return { ink: read('ink'), muted: read('muted'), line: read('line'), bg: read('bg'), panel: read('panel'),
           accent: read('accent'), warn: read('warn'), ok: read('ok'),
           dark: document.documentElement.style.colorScheme !== 'light' };
}

// One hue per topic, from a hash of its name, so a topic keeps its colour
// however the vocabulary around it changes; a paper changes colour only when
// its dominant topic does.
function topicColor(topic, dark) {
  if (!topic) return dark ? '#7a8090' : '#9aa0ab';
  let h = 2166136261;
  for (const ch of topic) { h ^= ch.codePointAt(0); h = Math.imul(h, 16777619) >>> 0; }
  const hue = h % 360;
  return dark ? `hsl(${hue} 50% 62%)` : `hsl(${hue} 55% 46%)`;
}

function graphRelationColor(relation, colors) {
  if (relation === 'contradicts') return colors.warn;
  if (relation === 'supports') return colors.ok;
  return colors.accent;
}

// Zoom and offset that show every node with a margin. Applied on each frame
// until the user zooms or pans, at which point the view is theirs.
function graphAutofit(w, h) {
  const nodes = [...GRAPH.nodes.values()];
  if (!nodes.length) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of nodes) {
    x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r);
    x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r);
  }
  const pad = 48;
  const zoom = Math.min(2, (w - 2 * pad) / Math.max(x1 - x0, 1), (h - 2 * pad) / Math.max(y1 - y0, 1));
  GRAPH.zoom = Math.max(0.2, zoom);
  GRAPH.tx = -((x0 + x1) / 2) * GRAPH.zoom;
  GRAPH.ty = -((y0 + y1) / 2) * GRAPH.zoom;
}

function graphDraw() {
  const { canvas, ctx } = GRAPH;
  if (!canvas || !ctx) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.width / dpr, h = canvas.height / dpr;
  if (GRAPH.autofit) graphAutofit(w, h);
  const colors = graphColors();
  ctx.save();
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.translate(w / 2 + GRAPH.tx, h / 2 + GRAPH.ty);
  ctx.scale(GRAPH.zoom, GRAPH.zoom);
  // A filter greys the papers it leaves out rather than removing them, so the
  // shape of the corpus stays visible behind the ones picked out. A label
  // dims the same way a topic does; a claim node carries neither and is left
  // alone by both.
  const dim = (n) => n.type === 'paper'
    && ((V.tag && !n.tags.has(V.tag)) || (V.label && !(n.labels || new Set()).has(V.label)));
  const hover = GRAPH.hover;
  const linked = new Set();
  if (hover) GRAPH.edges.forEach((e) => { if (e.source === hover) linked.add(e.target); if (e.target === hover) linked.add(e.source); });

  // Several ledger relations between one paper and one claim would be drawn
  // along one line, the later hiding the earlier; they are fanned out side by
  // side. (A topic edge under a tension is dropped in graphData instead.)
  const pairKey = (e) => `${e.a}|${e.b}`;
  const fan = new Map();
  for (const e of GRAPH.edges) {
    if (e.type !== 'ledger') continue;
    const key = pairKey(e);
    if (!fan.has(key)) fan.set(key, []);
    fan.get(key).push(e);
  }
  // Two papers can both disagree and cite, and the two lines run between the
  // same centres: drawn on top of each other the solid arrow fills the
  // tension's dashes and hides the layer underneath. Moved aside far enough
  // to read, which only arises for this one pair of types.
  const alsoTense = new Set(GRAPH.edges
    .filter((e) => e.type === 'tension')
    .map((e) => [pairKey(e), `${e.b}|${e.a}`]).flat());
  for (const e of GRAPH.edges) {
    const { source: a, target: b } = e;
    const touching = hover && (a === hover || b === hover);
    const faded = (hover && !touching) || dim(a) || dim(b);
    ctx.globalAlpha = faded ? 0.12 : 1;
    ctx.setLineDash([]);
    let ox = 0, oy = 0;
    if (e.type === 'ledger') {
      const siblings = fan.get(pairKey(e));
      if (siblings.length > 1) {
        const at = siblings.indexOf(e) - (siblings.length - 1) / 2;
        const d = Math.max(Math.hypot(b.x - a.x, b.y - a.y), 1);
        ox = (-(b.y - a.y) / d) * 4 * at; oy = ((b.x - a.x) / d) * 4 * at;
      }
    }
    if (e.type === 'cite') {
      ctx.strokeStyle = colors.accent;
      ctx.lineWidth = 1.2;
      let sx = 0, sy = 0;
      if (alsoTense.has(pairKey(e))) {
        const d = Math.max(Math.hypot(b.x - a.x, b.y - a.y), 1);
        sx = (-(b.y - a.y) / d) * 5; sy = ((b.x - a.x) / d) * 5;
      }
      ctx.beginPath(); ctx.moveTo(a.x + sx, a.y + sy); ctx.lineTo(b.x + sx, b.y + sy); ctx.stroke();
      graphArrow(ctx, { x: a.x + sx, y: a.y + sy }, { x: b.x + sx, y: b.y + sy, r: b.r },
                 colors.accent);
      continue;
    }
    if (e.type === 'topic') {
      ctx.strokeStyle = colors.muted;
      ctx.globalAlpha *= 0.25 + 0.09 * Math.min(e.w, 8);
      ctx.lineWidth = 0.8 + 0.25 * Math.min(e.w, 8);
    } else if (e.type === 'tension') {
      ctx.strokeStyle = colors.warn;
      ctx.lineWidth = 1.6 + 0.4 * Math.min(e.n, 4);
      if (e.open) ctx.setLineDash([6, 4]);
    } else {
      ctx.strokeStyle = graphRelationColor(e.relation, colors);
      ctx.lineWidth = 1.4;
      if (e.relation === 'contradicts') ctx.setLineDash([2, 3]);
    }
    ctx.beginPath(); ctx.moveTo(a.x + ox, a.y + oy); ctx.lineTo(b.x + ox, b.y + oy); ctx.stroke();
  }
  ctx.setLineDash([]);

  const showLabels = GRAPH.nodes.size <= 60;
  ctx.font = `${11 / GRAPH.zoom}px system-ui, sans-serif`;   // screen-constant label size
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (const n of GRAPH.nodes.values()) {
    const faded = (hover && n !== hover && !linked.has(n)) || dim(n);
    ctx.globalAlpha = faded ? 0.25 : 1;
    if (n.type === 'paper') {
      ctx.fillStyle = topicColor(n.topic, colors.dark);
      ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, 2 * Math.PI); ctx.fill();
      if (n === hover || (V.paper === n.key && !faded)) {
        ctx.strokeStyle = colors.ink; ctx.lineWidth = 2; ctx.stroke();
      }
    } else {
      ctx.fillStyle = colors.bg;
      ctx.strokeStyle = colors.ink;
      ctx.lineWidth = n === hover ? 2 : 1.2;
      ctx.beginPath(); ctx.rect(n.x - n.r, n.y - n.r, 2 * n.r, 2 * n.r); ctx.fill(); ctx.stroke();
    }
    if (showLabels || n === hover || linked.has(n)) {
      ctx.fillStyle = faded ? colors.muted : colors.ink;
      const text = n.type === 'paper' ? n.cite : n.key;
      ctx.fillText(text, n.x, n.y + n.r + 3);
    }
  }
  ctx.restore();
}

function graphLoop() {
  GRAPH.frame = 0;
  if (!GRAPH.canvas) return;
  if (GRAPH.alpha > 0) graphTick();
  graphDraw();
  if (GRAPH.alpha > 0 || GRAPH.drag) GRAPH.frame = requestAnimationFrame(graphLoop);
}

function graphHeat(alpha) {
  GRAPH.alpha = Math.max(GRAPH.alpha, alpha);
  if (!GRAPH.frame && GRAPH.canvas) GRAPH.frame = requestAnimationFrame(graphLoop);
}

// Screen -> world, in the canvas's CSS pixel space.
function graphWorld(event) {
  const rect = GRAPH.canvas.getBoundingClientRect();
  const sx = event.clientX - rect.left, sy = event.clientY - rect.top;
  return { x: (sx - rect.width / 2 - GRAPH.tx) / GRAPH.zoom, y: (sy - rect.height / 2 - GRAPH.ty) / GRAPH.zoom, sx, sy };
}

function graphNodeAt(x, y) {
  let best = null, bestD = Infinity;
  for (const n of GRAPH.nodes.values()) {
    const d = Math.hypot(n.x - x, n.y - y);
    const reach = n.r + 4 / GRAPH.zoom;
    if (d <= reach && d < bestD) { best = n; bestD = d; }
  }
  return best;
}

function graphTip(node, sx, sy) {
  const tip = $('content').querySelector('.graph-tip');
  if (!tip) return;
  if (!node) { tip.hidden = true; return; }
  if (node.type === 'paper') {
    const p = mapClient.state.papers.find((x) => x.key === node.key) || {};
    const topics = [...node.tags].sort().slice(0, 6).map((t) => `#${t}`).join(' ');
    tip.innerHTML = `<div>${esc(node.label)}</div>
      <div class="pm">${esc((p.authors || []).join(', ') || 'authors unknown')}${p.year ? ' · ' + esc(p.year) : ''}
        · ${node.n} claims${topics ? ' · ' + esc(topics) : ''}</div>`;
  } else {
    tip.innerHTML = `<div><span class="kind">my claim</span> ${esc(node.label)}</div><div class="pm"><code>${esc(node.key)}</code></div>`;
  }
  tip.hidden = false;
  const wrap = tip.parentElement.getBoundingClientRect();
  const { width, height } = tip.getBoundingClientRect();
  tip.style.left = `${Math.max(0, Math.min(sx + 14, wrap.width - width - 4))}px`;
  tip.style.top = `${Math.max(0, Math.min(sy + 14, wrap.height - height - 4))}px`;
}

function graphOpenPaper(key) {
  captureOpenEditor();
  closeEditorsNotBelongingTo(key);
  showView('claims');
  // The map leaves the papers a filter excludes on screen and clickable, so
  // the label has to give way here for a dimmed one to open onto anything.
  keepLabelFor(key);
  V.paper = key; V.tag = null; V.selectedId = null;
  syncHash(true);   // leaving the map is navigation: Back returns to it
  renderAll();
}

function graphBindCanvas(canvas) {
  canvas.addEventListener('mousedown', (event) => {
    if (event.button !== 0) return;
    const { x, y, sx, sy } = graphWorld(event);
    const node = graphNodeAt(x, y);
    GRAPH.moved = false;
    if (node) {
      GRAPH.drag = { node, dx: node.x - x, dy: node.y - y };
      node.fixed = true;
      GRAPH.autofit = false;   // or each frame refits the map under the pointer
      graphHeat(0.3);
    } else {
      GRAPH.pan = { sx, sy, tx: GRAPH.tx, ty: GRAPH.ty };
      GRAPH.autofit = false;
    }
    canvas.classList.add('drag');
    event.preventDefault();
  });
  canvas.addEventListener('mousemove', (event) => {
    const { x, y, sx, sy } = graphWorld(event);
    if (GRAPH.drag) {
      const n = GRAPH.drag.node;
      n.x = x + GRAPH.drag.dx; n.y = y + GRAPH.drag.dy;
      n.vx = n.vy = 0;
      GRAPH.moved = true;
      graphHeat(0.3);
      graphTip(null);
      return;
    }
    if (GRAPH.pan) {
      GRAPH.tx = GRAPH.pan.tx + (sx - GRAPH.pan.sx);
      GRAPH.ty = GRAPH.pan.ty + (sy - GRAPH.pan.sy);
      GRAPH.moved = true;
      graphDraw();
      return;
    }
    const node = graphNodeAt(x, y);
    if (node !== GRAPH.hover) {
      GRAPH.hover = node;
      canvas.classList.toggle('hover', !!node);
      graphDraw();
    }
    graphTip(node, sx, sy);
  });
  const release = () => {
    if (GRAPH.drag) { GRAPH.drag.node.fixed = false; GRAPH.drag = null; graphHeat(0.1); }
    GRAPH.pan = null;
    canvas.classList.remove('drag');
  };
  canvas.addEventListener('mouseup', release);
  canvas.addEventListener('mouseleave', () => {
    release();
    if (GRAPH.hover) { GRAPH.hover = null; canvas.classList.remove('hover'); graphDraw(); }
    graphTip(null);
  });
  canvas.addEventListener('click', (event) => {
    if (GRAPH.moved) return;   // a drag that ended on the node is not a click
    const { x, y } = graphWorld(event);
    const node = graphNodeAt(x, y);
    if (node && node.type === 'paper') graphOpenPaper(node.key);
  });
  canvas.addEventListener('wheel', (event) => {
    event.preventDefault();
    const { sx, sy } = graphWorld(event);
    const rect = canvas.getBoundingClientRect();
    const factor = Math.exp(-event.deltaY * 0.0015);
    const next = Math.min(6, Math.max(0.2, GRAPH.zoom * factor));
    GRAPH.autofit = false;
    // Zoom about the cursor: the world point under it stays put.
    const cx = sx - rect.width / 2, cy = sy - rect.height / 2;
    GRAPH.tx = cx - (cx - GRAPH.tx) * (next / GRAPH.zoom);
    GRAPH.ty = cy - (cy - GRAPH.ty) * (next / GRAPH.zoom);
    GRAPH.zoom = next;
    graphDraw();
  }, { passive: false });
}

function graphFit() {
  const wrap = $('content').querySelector('.graph-wrap');
  const canvas = GRAPH.canvas;
  if (!wrap || !canvas) return;
  const main = $('main');
  // Fill what is left of the pane below the header, so the map is on screen
  // whole rather than scrolling; never so short that it is useless.
  const mainRect = main.getBoundingClientRect();
  const top = wrap.getBoundingClientRect().top - mainRect.top + main.scrollTop;
  // A wrapped header can put the pane's CSS bottom below the actual viewport.
  const visibleHeight = Math.min(main.clientHeight, window.innerHeight - mainRect.top);
  const height = Math.max(288, visibleHeight - top - 16);
  wrap.style.height = `${height}px`;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(wrap.clientWidth * dpr);
  canvas.height = Math.round(height * dpr);
  graphDraw();
}

function graphHeader() {
  const opts = V.graph;
  return `<div class="paperhead">
    <h2>How the papers connect</h2>
    <p class="ps">Each circle is a paper, sized by how many claims it makes and coloured by the
      topic it speaks to most. Squares are your own claims. Drag a node to move it, scroll to
      zoom, drag the background to pan, and click a paper to read it.</p>
    <div class="graph-legend">
      <span><i></i>claims share a topic</span>
      <span><i class="tension"></i>papers disagree (dashed while open)</span>
      <span><i class="cite"></i>cites, arrow to the paper cited</span>
      <span><i class="supports"></i>supports my claim</span>
      <span><i class="contradicts"></i>contradicts it</span>
      <span><i class="other"></i>refines it or supplies a method</span>
      <span>(links marked independent are not drawn)</span>
    </div>
    <div class="row graph-controls">
      <label><input type="checkbox" data-graph-opt="topics" ${opts.topics ? 'checked' : ''}> shared topics</label>
      <label title="Hide topic edges thinner than this">at least
        <input type="range" min="1" max="${Math.max(1, GRAPH.maxShared || 1)}" value="${GRAPH.minShared || 1}" data-graph-opt="minShared">
        <span data-graph-min>${GRAPH.minShared || 1}</span> shared</label>
      <label><input type="checkbox" data-graph-opt="tensions" ${opts.tensions ? 'checked' : ''}> tensions</label>
      <label><input type="checkbox" data-graph-opt="cites" ${opts.cites ? 'checked' : ''}> citations</label>
      <label><input type="checkbox" data-graph-opt="ledger" ${opts.ledger ? 'checked' : ''}> my claims</label>
      <span class="hint" data-graph-count style="margin-left:auto"></span>
    </div>
  </div>`;
}

// A head on the cited paper's end of a citation, just off its edge, so which
// way the citation runs is readable without hovering.
function graphArrow(ctx, from, to, color) {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const distance = Math.max(Math.hypot(dx, dy), 1);
  const ux = dx / distance;
  const uy = dy / distance;
  const tipX = to.x - ux * (to.r + 1);
  const tipY = to.y - uy * (to.r + 1);
  const size = 6;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(tipX, tipY);
  ctx.lineTo(tipX - ux * size - uy * size * 0.5, tipY - uy * size + ux * size * 0.5);
  ctx.lineTo(tipX - ux * size + uy * size * 0.5, tipY - uy * size - ux * size * 0.5);
  ctx.closePath();
  ctx.fill();
}

function graphStatus() {
  const count = $('content').querySelector('[data-graph-count]');
  if (!count) return;
  const papers = [...GRAPH.nodes.values()].filter((n) => n.type === 'paper').length;
  const bits = [`${papers} papers`, `${GRAPH.edges.length} links`];
  if (V.tag) bits.push(`highlighting #${V.tag}`);
  count.textContent = bits.join(' · ');
  const range = $('content').querySelector('[data-graph-opt="minShared"]');
  if (range) {
    range.max = String(Math.max(1, GRAPH.maxShared || 1));
    if (!V.graph.minShared) range.value = String(GRAPH.minShared || 1);
  }
  const label = $('content').querySelector('[data-graph-min]');
  if (label) label.textContent = String(GRAPH.minShared || 1);
}

// Drawn in two steps so the poll can update the map in place. The first call
// builds the header, controls and canvas; later calls only refresh the data,
// keeping the layout, zoom, and whatever the pointer is doing.
function renderGraph() {
  const content = $('content');
  let wrap = content.querySelector('.graph-wrap');
  if (!wrap || !GRAPH.canvas) {
    graphStop();
    GRAPH.signature = '';
    graphSync();
    content.innerHTML = graphHeader()
      + (mapClient.state.papers.length ? '' : '<p class="empty">Nothing to map yet. Add a paper or two and come back.</p>')
      + '<div class="graph-wrap"><canvas></canvas><div class="graph-tip" hidden></div></div>';
    wrap = content.querySelector('.graph-wrap');
    GRAPH.canvas = wrap.querySelector('canvas');
    GRAPH.ctx = GRAPH.canvas.getContext('2d');
    graphBindCanvas(GRAPH.canvas);
    // Watch the pane, not the wrapper: graphFit sets the wrapper's height
    // itself, so a vertical-only window resize changes the pane and leaves the
    // wrapper's box as it was, which would never fire an observer on it.
    GRAPH.observer = new ResizeObserver(() => graphFit());
    GRAPH.observer.observe($('main'));
    GRAPH.observer.observe(document.querySelector('header'));
    GRAPH.autofit = true;
    graphFit();
    graphHeat(1);
  } else if (graphSync()) {
    graphHeat(0.4);
  } else {
    graphDraw();
  }
  graphStatus();
}

document.getElementById('graph-nav').addEventListener('click', (event) => {
  if (!event.target.closest('[data-view]')) return;
  showView('graph');
  syncHash(true);
  renderAll();
  loadCitations();
});

// For the browser tests and anyone poking at the console: where things are.
window.doxographGraph = () => ({
  nodes: [...GRAPH.nodes.values()].map((n) => ({ id: n.id, type: n.type, key: n.key, x: n.x, y: n.y, r: n.r, topic: n.topic })),
  edges: GRAPH.edges.map((e) => ({ type: e.type, a: e.a, b: e.b, w: e.w, n: e.n, relation: e.relation })),
  zoom: GRAPH.zoom, tx: GRAPH.tx, ty: GRAPH.ty, alpha: GRAPH.alpha,
});

document.addEventListener('doxograph:appearance', () => {
  if (GRAPH.canvas) graphDraw();
});
