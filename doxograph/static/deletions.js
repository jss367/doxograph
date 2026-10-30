'use strict';

// Deletes held for their undo window, and the state they are kept out of.
// Loaded before app.js: its workspace controller hands `flushTrash` over at
// startup. Everything else here runs only after app.js has loaded.

// --- deletions, undone by not sending them --------------------------------
//
// A delete waits out its notice before it is sent, so Undo is simply not
// sending it. Deleting at once and recreating on Undo would hand back a
// different claim: a new id, and the tensions, agreements and syntheses that
// cite the old one gone for good. Until it is sent, the row is kept out of
// what the page draws, so the wait is invisible.
const UNDO_MS = 8000;
const trash = new Map();   // "kind:id" -> the request that is waiting

function trashed(kind, id) {
  return trash.has(`${kind}:${id}`);
}

// `pruneTrashed` takes rows out of the state the page is holding, so that
// state no longer matches the tag the server gave it. Undoing a delete the
// server never heard about changes nothing on its side, and the next poll
// would be answered "not modified" and leave the row missing for good.
function forgetStateTag() {
  workspaceClient.invalidateState();
}

// Whether a claim still says what a synthesis's basis on file recorded it
// saying. The server writes each entry of that basis as a JSON list of the
// three fields the model was shown, so it is read back rather than built again
// here: building it would mean reproducing Python's spacing and its escaping of
// everything above ASCII, and what the comparison is about is the values.
// The heading a cross-paper prompt writes for a claim's paper, as
// `store.cite_head` writes it. The fingerprints below were made on the server
// and are compared here character for character, so the two have to agree: a
// blank author is skipped rather than shown, and a paper with none is named by
// its key.
function citeHead(claim) {
  const authors = claim.paper_authors || [];
  const key = claim.paper || '';
  let head = key;
  for (const author of authors) {
    const parts = String(author).split(/\s+/).filter(Boolean);
    if (parts.length) { head = parts[parts.length - 1]; break; }
  }
  if (authors.length > 1) head += ' et al.';
  return `${head} (${claim.paper_year || 'n.d.'}): ${claim.paper_title || key}`;
}

function basisMatches(fingerprint, claim) {
  let written;
  try { written = JSON.parse(fingerprint); } catch { return false; }
  return Array.isArray(written) && written.length === 4
    && written[0] === (claim.text || '') && written[1] === (claim.evidence || '')
    && written[2] === (claim.kind || 'finding') && written[3] === citeHead(claim);
}

// `synthesis_rows` reads `stale` off two comparisons: the topic's claims
// against the basis the text was written from, and the topic's tensions against
// the ones the model was shown. A held claim can settle either of them as
// easily as unsettle it — a synthesis reading stale only because a claim had
// been added since is current again once that claim is the one deleted — so the
// flag is worked out again rather than simply turned on. `tensions` is the
// pruned list: one citing a held claim is already out of it, as it is out of
// the answer the server would give.
function synthesisStale(row, live, tensions) {
  const basis = row.claims || {};
  if (Object.keys(basis).length !== live.length
      || !live.every((claim) => claim.id in basis && basisMatches(basis[claim.id], claim))) {
    return true;
  }
  const shown = (tensions || []).filter(
    (tension) => (tension.topics || []).includes(row.topic) && tension.status !== 'dismissed');
  const written = row.tensions || {};
  if (Object.keys(written).length !== shown.length) return true;
  // A record written before a field was part of the basis holds a shorter
  // list, which matches nothing here and leaves it reading stale, as it does
  // on the server: what the model was told is not known.
  return !shown.every((tension) => {
    const seen = written[tension.id];
    return Array.isArray(seen) && seen.length === 4
      && seen[0] === (tension.kind ?? null) && seen[1] === (tension.status ?? null)
      && seen[2] === Boolean(tension.stale) && seen[3] === (tension.note || '');
  });
}

// Takes the waiting rows out of `S`. Called wherever `S` is replaced, and
// written to be safe to run twice on the same state: the per-paper counts are
// recounted from the claims that are left rather than decremented.
function pruneTrashed() {
  if (!trash.size) return;
  const all = S.claims || [];
  const kept = all.filter((row) => !trashed('claim', row.id));
  if (kept.length !== all.length) {
    S.claims = kept;
    const counts = new Map();
    kept.forEach((row) => {
      const count = counts.get(row.paper) || { claims: 0, unreviewed: 0, unverified: 0 };
      count.claims += 1;
      if (!row.reviewed) count.unreviewed += 1;
      if (row.quote_verified === false) count.unverified += 1;
      counts.set(row.paper, count);
    });
    (S.papers || []).forEach((paper) => {
      const count = counts.get(paper.key) || { claims: 0, unreviewed: 0, unverified: 0 };
      paper.n_claims = count.claims;
      paper.n_unreviewed = count.unreviewed;
      paper.n_unverified = count.unverified;
      // The status dot is the last of the per-paper summary to be recounted.
      // `delete_claim` calls `refresh_status`, so the paper the server answers
      // with once the wait is over reads its status off the claims that are
      // left; holding the old one shows a paper with no new claims still
      // marked as waiting to be reviewed, or a paper with nothing left on it
      // still marked reviewed. Worked out the way `refresh_status` works it
      // out, which needs to know whether the paper was ever read by the model
      // for the case where the held claim was its last.
      paper.status = count.claims === 0 ? (paper.has_extraction ? 'extracted' : 'fetched')
        : count.unreviewed ? 'extracted' : 'reviewed';
    });
    // The topic sidebar draws `tag_counts` as the server sent it, so a held
    // claim would go on being counted under every topic it carries — and a
    // topic whose only claim is held would stay on the list at one, offering a
    // filter that draws nothing. Recounted from what is left, like the per-paper
    // counts above and in the server's own order — commonest first, ties by
    // name — so the list does not reshuffle itself for the length of the wait.
    const tags = new Map();
    kept.forEach((row) => (row.tags || []).forEach(
      (tag) => tags.set(tag, (tags.get(tag) || 0) + 1)));
    const counted = Object.fromEntries([...tags].sort(
      (a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)));
    // One exception to counting what is left: the topic being filtered on. Its
    // entry in the sidebar is the only way back out of that filter, so dropping
    // it for the length of the wait would leave the reader on a view with no
    // claims and nothing to click — and clearing `V.tag` instead would make
    // Undo a one-way trip, putting them back on the whole corpus rather than
    // on the topic they were reading. Kept at zero, and only if it was there
    // before the prune, so a topic named by a stale URL is still dropped by
    // `dropMissingFilters`. The delete going through takes the entry with it:
    // the trash is empty by then, this runs no more, and the filter goes with
    // the count the server sends.
    if (V.tag && !Object.hasOwn(counted, V.tag)
        && Object.hasOwn(S.tag_counts || {}, V.tag)) counted[V.tag] = 0;
    S.tag_counts = counted;
  }
  // The analysis views hold the server's own join: each tension and agreement
  // carries the claim rows it cites, not their ids. Taking a held claim out of
  // `S.claims` alone would leave those copies on screen, still open to a
  // decision or a quote, so they are filtered the way `tension_rows` and
  // `agreement_rows` filter them — a tension needs both of its claims, an
  // agreement needs members from two papers. A tension that keeps both of its
  // claims keeps its topics too: they are the ones both of them carry, and both
  // of them are still here.
  S.tensions = (S.tensions || []).filter(
    (row) => !(row.claims || []).some((claim) => trashed('claim', claim.id)));
  let regrouped = false;
  S.agreements = (S.agreements || [])
    .filter((row) => !trashed('agreement', row.id))
    .map((row) => {
      const members = (row.claims || []).filter((claim) => !trashed('claim', claim.id));
      if (members.length === (row.claims || []).length) return row;
      regrouped = true;
      // A group that loses a member can gain a topic. `agreement_rows` files
      // the topics under the agreement and then shows only the ones every
      // member still carries, so a topic that one member alone had dropped is
      // missing from the row the server sent; take that member out and the
      // topic is good for everyone left, which is what the server will say
      // once the delete lands. Keeping the filtered list would hide the group
      // from its own topic for the length of the wait and then hand it back at
      // the end. The page cannot un-filter a list, so the row carries the set
      // the server filtered from, `topics_on_file`, and it is filtered again
      // here against the members that are left.
      //
      // Stale, as on the server: the note and the decision on file were about
      // a group that is not the one on screen.
      const topics = (row.topics_on_file || row.topics || []).filter(
        (topic) => members.every((claim) => (claim.tags || []).includes(topic)));
      return { ...row, claims: members, topics,
               n_papers: new Set(members.map((c) => c.paper)).size, stale: true };
    })
    .filter((row) => new Set((row.claims || []).map((claim) => claim.paper)).size >= 2);
  if (regrouped) {
    // `agreement_rows` orders by status, then by how many papers are in the
    // group, then by when it was found. A group that lost a member to the wait
    // is smaller than the one the server sorted, so the cards would shuffle
    // themselves the moment the wait ended and the real answer arrived. Sorted
    // here on the same key, and stably, so rows the delete did not touch keep
    // the order they came in.
    const statuses = S.tension_statuses || ['open', 'confirmed', 'dismissed'];
    const rank = (row) => (statuses.indexOf(row.status) + 1 || 10) - 1;
    const found = (row) => row.found || '';
    S.agreements.sort((a, b) => rank(a) - rank(b) || b.n_papers - a.n_papers
      || (found(a) < found(b) ? -1 : found(a) > found(b) ? 1 : 0));
  }
  // A synthesis is the same kind of derived row: `synthesis_rows` works out the
  // counts and the stale flag from the claims its topic has now, not from the
  // ids on file, and drops a topic that has none left. Held claims were taken
  // out of only the syntheses deleted by topic, so a topic one of them carried
  // went on claiming more claims and papers than it still had and cited a claim
  // the page no longer knows — `citeHtml` leaves that bracket as written, a
  // bare `[paper-a-c1]`. So the rows a held claim touches are rebuilt the way
  // the server builds them: no claims left and the card goes, otherwise recount
  // and read the stale flag off the basis on file. Undo is still the delete
  // never being sent, so the next refresh brings the row back as the server
  // has it.
  const heldTopics = new Set();
  all.forEach((row) => {
    if (trashed('claim', row.id)) (row.tags || []).forEach((tag) => heldTopics.add(tag));
  });
  S.syntheses = (S.syntheses || [])
    .filter((row) => !trashed('synthesis', row.topic))
    .map((row) => {
      if (!heldTopics.has(row.topic)) return row;
      const live = kept.filter((claim) => (claim.tags || []).includes(row.topic));
      if (!live.length) return null;
      return { ...row, n_claims: live.length,
               n_papers: new Set(live.map((claim) => claim.paper)).size,
               stale: synthesisStale(row, live, S.tensions) };
    })
    .filter(Boolean);
}

async function deleteLater(kind, id, path, label, { paper = null } = {}) {
  const token = `${kind}:${id}`;
  if (trash.has(token)) return;
  const workspace = workspaceClient.workspaceId || 'default';
  let notice = null;
  // The entry stays in the trash until the request settles, so the row does not
  // flash back on screen between the notice fading and the server answering.
  // That leaves it visible to a second caller — the notice running out while an
  // export flushes, say — so the request itself is what is shared: one delete,
  // and no second one to come back 404 and leave a failure notice standing.
  // It lives on the entry rather than in here, because the page closing has to
  // see it too and has only the entry to look at.
  const send = () => {
    const entry = trash.get(token);
    if (!entry) return Promise.resolve();   // already sent, or undone
    if (entry.sending) return entry.sending;
    if (notice) notice.close();
    // Resolves to whether the row is really gone. A delete that failed leaves
    // it on file, and the callers waiting on this — an export, a retag, a model
    // pass — would go on to read a corpus that still holds a claim the reader
    // watched disappear. They are told, rather than left to assume it settled.
    entry.sending = (async () => {
      let sent = true;
      try {
        // Named rather than implied: `flushTrash` sends these before a switch,
        // and this makes that a guarantee rather than an ordering to preserve.
        //
        // `keepalive` because the page closing stands aside for this request
        // rather than sending a second one: an ordinary fetch can be abandoned
        // as the page goes, and the row would come back from a delete the
        // reader watched happen. A DELETE carries no body, so the size limit
        // that comes with it costs nothing.
        await api(path, {
          method: 'DELETE',
          keepalive: true,
          headers: { 'X-Doxograph-Workspace': workspace },
        });
      } catch (error) {
        sent = false;
        toast(`Could not delete ${label}: ${error.message}`, { tone: 'warn', timeout: 0 });
      } finally {
        trash.delete(token);
        forgetStateTag();
      }
      await refreshAll();
      return sent;
    })();
    return entry.sending;
  };
  trash.set(token, { path, workspace, paper, send, sending: null });
  forgetStateTag();
  pruneTrashed();
  // `pruneTrashed` only takes the row out of the state the page holds; what is
  // on screen stands until something draws it. That draw happens here, before
  // the refresh, because the refresh is a read from the server and a server
  // restarting under the click rejects it — and then nothing redraws at all.
  // The row would sit there under the "Deleted …" notice, still clickable and
  // still open to an edit or a second delete, until the timer sent the DELETE
  // out from under it.
  renderAll();
  // The redraw comes before the notice, so the row is off the screen before an
  // offer to put it back appears over it. The refresh can still fail, and the
  // notice is what carries the timer that finally sends the delete: raised only
  // on the way out of a refresh that worked, it would leave the entry sitting in
  // the trash with the row hidden, the DELETE never sent, and no Undo and no
  // error to say so. So it goes up either way, and the failure still reaches
  // the caller.
  try {
    await refreshAll();
  } finally {
    // The wait can end during that refresh — `flushTrash` sends what is waiting
    // — and a notice raised afterwards would offer to undo a delete the server
    // already has. The entry is still in the trash while its request is in
    // flight, which is what keeps the row off the screen, so `sending` is the
    // thing to ask: once it is set there is nothing left to undo.
    if (trash.get(token) && !trash.get(token).sending) {
      notice = toast(`Deleted ${label}.`, {
        timeout: UNDO_MS,
        onExpire: send,
        actions: [{
          label: 'Undo',
          onClick: async () => {
            // Sent between the notice going up and this being clicked. `send`
            // closes the notice, so this is all but unreachable — but an Undo
            // that quietly does nothing is worse than one that says so.
            if (trash.get(token)?.sending) {
              toast(`That delete has already been sent; ${label} is gone.`, { tone: 'warn' });
              return;
            }
            trash.delete(token);
            forgetStateTag();
            await refreshAll();
          },
        }],
      });
    }
  }
}

// A delete belongs to the workspace it was made in, so leaving one sends what
// is still waiting rather than carrying it across, where the same claim id can
// name a different claim. It is also what the server has to be told before it
// is asked to work anything out from the corpus: for those eight seconds the
// row is gone from the page but still on file, and an export or a model pass
// started meanwhile would take it as live.
// Settle the held deletes, then say which workspace the action was asked in.
// The flush ends in a read and a read is not counted as a change in flight, so
// the picker can move in the gap; anything sent afterwards has to name the
// corpus the reader was looking at, not the one they have since gone to.
// Null when a held delete came back an error: the row is still on file, the
// reader has already been told so, and whatever was waiting on the deletion
// must not go ahead against a corpus that still holds it. Every caller checks.
async function settleDeletes(wanted = null) {
  const workspace = workspaceClient.workspaceId;
  if (!await flushTrash(wanted)) return null;
  return { 'X-Doxograph-Workspace': workspace };
}

// True when everything it sent is really gone.
async function flushTrash(wanted = null) {
  // Drained rather than snapshotted: the page stays interactive while this
  // runs, so a row deleted during it has to go with the rest. A snapshot would
  // leave that one on file for the export or the model pass waiting on this.
  //
  // This workspace's rows, though. Each delete leaves the trash before its own
  // trailing read, and a read is not counted as a change in flight, so the
  // picker can move while the drain is between rounds. A delete made after
  // that belongs to the corpus the reader has gone to: taking it here would
  // send it under an Undo the page is still offering, and its failure would
  // call off an action in a workspace it has nothing to do with. It is left
  // to its own eight seconds, and to the flush that leaving that workspace
  // runs.
  const workspace = workspaceClient.workspaceId || 'default';
  let settled = true;
  for (;;) {
    const waiting = [...trash.values()]
      .filter((entry) => entry.workspace === workspace && (!wanted || wanted(entry)));
    if (!waiting.length) return settled;
    // `send` resolves to `undefined` for an entry that went while this was
    // deciding; only an outright `false` is a failure.
    const sent = await Promise.all(waiting.map((entry) => entry.send()));
    if (sent.some((ok) => ok === false)) settled = false;
  }
}

// Coming back to a page the browser froze rather than unloaded. `pagehide`
// sent the held deletes, but the DOM came back as it was: the notices on it
// offer to undo work that is finished, and their Undo would have nothing to
// take back. They go, and the corpus is read again from the server.
window.addEventListener('pageshow', (event) => {
  if (!event.persisted) return;
  $('toasts').innerHTML = '';
  forgetStateTag();
  refreshAll();
});

// A tab closed while a delete is still waiting sends it now rather than
// silently dropping it: the row is already gone from the page, and coming back
// to find it restored would be the app forgetting what it was told.
window.addEventListener('pagehide', () => {
  trash.forEach((entry) => {
    // One already on its way needs nothing: a second request would race it, and
    // whichever lost would come back 404 over a row that was deleted after all.
    if (entry.sending) return;
    const headers = { 'X-Doxograph-Workspace': entry.workspace };
    workspaceClient.response(entry.path, { method: 'DELETE', headers, keepalive: true })
      .catch(() => {}); // The page is leaving; there is no remaining UI for a failure.
  });
  trash.clear();
});
