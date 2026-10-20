// Streams of requests, and the pick on the list: three requests, A and B with a stream of their own (fragments marked A
// and B), C without, plus the board's own stream (marked O).
//  - the list marks A and B with a checkbox and C with none; until this device picks one, the board's own plays and is
//    the one checked
//  - a click on A's checkbox plays A's stream (the link is not followed), and the pick is kept in this browser; a click
//    on its label again unchecks it: no stream at all; B's checkbox then plays B
//  - A's page plays A's own stream whatever the list's pick; Back to the list plays B again; C's page plays none
//  - another frame of this browser (another tab) follows a pick made here
//  - B's stream closed: the list falls back to the board's own (the pick of B is kept, for when it comes back); A
//    completed: its stream ends with it and its row has no checkbox
__t.run(async () => {
    const { sleep, until, pane, api, drop } = __t;
    const out = { T0: Date.now() };
    const mk = async (title) => (await api('POST', '/api/requests', { title, tasks: ['one', 'two'] })).data.id;
    const ids = { c: await mk('C has no stream'), b: await mk('B watches a test rig'), a: await mk('A prints a bracket') };
    out.ids = ids;
    out.putA = (await api('PUT', '/api/requests/' + ids.a + '/stream', { url: 'http://fake.invalid/live?marker=A' })).status;
    out.putB = (await api('PUT', '/api/requests/' + ids.b + '/stream', { url: 'http://fake.invalid/live?marker=B' })).status;
    out.own = (await api('POST', '/api/stream', { url: 'http://fake.invalid/live?marker=O' })).status;
    out.sids = Object.fromEntries((await api('GET', '/api/stream')).data.streams.map((s) => [s.request_id || 'own', s.id]));
    const loaded = (f) => new Promise((resolve) => f.addEventListener('load', resolve, { once: true }));
    const doc = (f) => f.contentDocument;
    const row = (f, id) => doc(f).querySelector('#list-active a.row[href="/r/' + id + '"], #list-finished a.row[href="/r/' + id + '"]');
    const pickOf = (f, id) => { const r = row(f, id); const p = r && r.querySelector('.pick'); return p && !p.hidden ? p : null; };
    const picks = (f) => {
        const o = {};
        for (const [k, id] of Object.entries(ids)) { const p = pickOf(f, id); o[k] = p ? { on: p.querySelector('input').checked, text: p.textContent, state: p.dataset.state || null } : null; }
        const own = doc(f).getElementById('board-pick');
        o.own = own.hidden ? null : { on: doc(f).getElementById('board-pick-box').checked, text: own.textContent };
        return o;
    };
    // The marker of the fragments the frame's player was given last (the stream it plays), and the stored pick.
    const media = (f) => f.contentWindow.__rec.media;
    const lastMarker = (f) => { const fr = media(f).fragments; return fr.length ? fr[fr.length - 1].marker : null; };
    const stored = (f) => { try { return JSON.parse(f.contentWindow.localStorage.getItem('taskstatus.settings.local') || '{}').stream_pick ?? null; } catch (e) { return String(e); } };
    const playing = (f, marker) => until(() => pane(f).state === 'video' && lastMarker(f) === marker, 12000);
    const snap = (f) => ({ path: f.contentWindow.location.pathname, pane: pane(f), marker: lastMarker(f), picks: picks(f), stored: stored(f) });
    const viewers = async () => Object.fromEntries((await api('GET', '/api/stream')).data.streams.map((s) => [s.request_id || 'own', s.viewers]));

    // 1. none picked yet: the board's own plays, checked; A and B unchecked; C has no checkbox
    const f = await __t.frame('/', 1280, 700, { __media: 'stub', __autoplay: 'allowed', __keep: '1' });  // a fresh profile: nothing to keep but its own
    await until(() => pickOf(f, ids.a) && pickOf(f, ids.b), 8000);
    await playing(f, 'O');
    await sleep(400);
    out.auto = snap(f);

    // 2. A's checkbox: A plays, the page stays on the list
    pickOf(f, ids.a).querySelector('input').click();
    await sleep(50);
    out.clickedA = { path: f.contentWindow.location.pathname, picks: picks(f), stored: stored(f) };
    await playing(f, 'A');
    await sleep(600);
    out.pickedA = snap(f);
    for (let i = 0; i < 30; i++) { out.viewersA = await viewers(); if (out.viewersA[ids.a] === 1 && out.viewersA.own === 0) break; await sleep(250); }

    // 3. A's label again (its text, not the box): unchecked, nothing plays
    pickOf(f, ids.a).querySelector('.pick-text').click();
    await until(() => !pane(f).streaming, 8000);
    await sleep(300);
    out.none = snap(f);

    // 4. B's checkbox
    pickOf(f, ids.b).querySelector('input').click();
    await playing(f, 'B');
    await sleep(300);
    out.pickedB = snap(f);

    // 5. another frame of this browser follows a pick made in the first
    const g = await __t.frame('/', 640, 700, { __media: 'stub', __autoplay: 'allowed', __keep: '1' });
    await playing(g, 'B');
    out.otherB = snap(g);
    doc(g).getElementById('board-pick-box').click();
    await playing(f, 'O');
    await sleep(300);
    out.followed = { f: snap(f), g: snap(g) };
    drop(g);
    pickOf(f, ids.b).querySelector('input').click();
    await playing(f, 'B');

    // 6. A's page plays A's own stream; Back to the list plays the pick (B) again
    let there = loaded(f);
    row(f, ids.a).querySelector('.title').click();
    await there;
    await until(() => doc(f).querySelector('#task-list .row'), 8000);
    // No stub on a page reached by a link (the real player, which may not decode the fake's frames): the media it
    // asked the board for, and the "connecting to" line, tell which stream it plays.
    await until(() => pane(f).streaming && performanceMedia(f).length, 12000);
    await sleep(800);
    out.pageA = { path: f.contentWindow.location.pathname, streaming: pane(f).streaming, line: pane(f).line, fetch: performanceMedia(f) };
    const back = loaded(f);
    f.contentWindow.history.back();
    await back;
    await until(() => pickOf(f, ids.b), 8000);
    await playing(f, 'B');     // the list page again, with its stub (from the page cache, or loaded anew from its URL)
    await sleep(300);
    out.backB = snap(f);

    // 7. C's page: no stream
    there = loaded(f);
    row(f, ids.c).querySelector('.title').click();
    await there;
    await until(() => doc(f).querySelector('#task-list .row') && doc(f).getElementById('conn').dataset.state === 'live', 8000);
    await sleep(1500);
    out.pageC = { path: f.contentWindow.location.pathname, streaming: pane(f).streaming, hidden: pane(f).hidden, fetch: performanceMedia(f) };
    drop(f);

    // 8. B's stream closed: the list falls back to the board's own, the pick of B kept; A completed: its stream ends too
    const h = await __t.frame('/', 1280, 700, { __media: 'stub', __autoplay: 'allowed', __keep: '1' });
    await playing(h, 'B');
    out.closeB = (await api('DELETE', '/api/requests/' + ids.b + '/stream')).data.closed;
    await playing(h, 'O');
    await sleep(300);
    out.fallback = snap(h);
    out.doneA = (await api('POST', '/api/requests/' + ids.a + '/complete', { status: 'done' })).data.stream;
    await until(() => row(h, ids.a) && row(h, ids.a).closest('#list-finished'), 8000);
    await sleep(300);
    out.finishedA = snap(h);
    out.streamsLeft = (await api('GET', '/api/stream')).data.streams.map((s) => s.request_id);
    out.errors = [h.contentWindow.__errors.slice()];
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);

    // The stream ids a page asked the media route for, in order (Resource Timing; a page reached by a link has no stub).
    function performanceMedia(fr) {
        try {
            return fr.contentWindow.performance.getEntriesByType('resource').map((e) => e.name).filter((n) => n.includes('/api/stream/media'))
                .map((n) => new URL(n).searchParams.get('id'));
        } catch (e) { return String(e); }
    }
});
