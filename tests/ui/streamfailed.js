// A stream the board gives up on (this case's board runs with --stream-give-up 3): a source that refuses every connection.
//  - the list: while it connects, A's row has its checkbox; once the board closes it, the checkbox goes and a red
//    "Stream failed" takes its place, its tooltip saying which stream, when and why
//  - A's page says it too, with how long and why in the note itself, and plays nothing
//  - a new stream for A: the note goes and the pane plays it
//  - the board's own stream: "Board stream failed" in the bar above the list, until a new one of its own opens
__t.run(async () => {
    const { sleep, until, pane, api } = __t;
    const out = { T0: Date.now() };
    const a = (await api('POST', '/api/requests', { title: 'A watches a camera that is off', tasks: ['one'] })).data.id;
    out.id = a;
    out.put = (await api('PUT', '/api/requests/' + a + '/stream', { url: 'http://user:s3cr3t@fake.invalid/refuse?for=A' })).status;
    let f = await __t.frame('/', 1280, 700, { __media: 'stub' });
    const doc = () => f.contentDocument, win = () => f.contentWindow;
    const row = () => doc().querySelector('#list-active a.row[href="/r/' + a + '"]');
    const shown = (el) => !!el && !el.hidden && win().getComputedStyle(el).display !== 'none';
    const note = (el) => el && { shown: shown(el), text: el.textContent, title: el.getAttribute('title'), color: win().getComputedStyle(el).color };
    const rowState = () => {
        const r = row();
        return r && { pick: shown(r.querySelector('.pick')), failed: note(r.querySelector('.stream-failed')) };
    };
    const errors = [];
    const swap = async (path) => {
        errors.push((win().__errors || []).slice());
        __t.drop(f);
        f = await __t.frame(path, 1280, 700, { __media: 'stub' });
    };

    // 1. connecting: the checkbox, no note
    await until(() => rowState() && rowState().pick, 8000);
    out.connecting = rowState();
    // 2. given up: the note in its place
    out.failedAfter = await until(() => rowState() && rowState().failed.shown, 12000) ? Date.now() - out.T0 : null;
    await sleep(300);
    out.failed = rowState();
    out.failedVar = win().getComputedStyle(doc().documentElement).getPropertyValue('--failed').trim();
    out.listPane = pane(f);
    out.api = (await api('GET', '/api/requests/' + a)).data.stream_failed;

    // 3. A's page
    await swap('/r/' + a);
    const head = () => doc().querySelector('#req-head .stream-failed');
    await until(() => head() && shown(head()), 8000);
    await sleep(300);
    out.page = Object.assign(note(head()), { why: head().querySelector('.why').textContent, pane: pane(f) });

    // 4. a new stream for A: the note goes, the pane plays it
    out.again = (await api('PUT', '/api/requests/' + a + '/stream', { url: 'http://fake.invalid/live?marker=A' })).status;
    await until(() => !shown(head()) && pane(f).streaming, 8000);
    await sleep(300);
    out.cleared = { note: shown(head()), streaming: pane(f).streaming };

    // 5. the board's own, on the list
    await api('DELETE', '/api/requests/' + a + '/stream');
    out.own = (await api('POST', '/api/stream', { url: 'http://fake.invalid/refuse?for=own' })).status;
    await swap('/');
    const bar = () => ({ bar: shown(doc().getElementById('board-pick')), label: shown(doc().getElementById('board-pick-label')),
                         failed: note(doc().getElementById('board-failed')) });
    await until(() => bar().label, 8000);
    out.ownConnecting = bar();
    await until(() => bar().failed.shown, 12000);
    await sleep(300);
    out.ownFailed = Object.assign(bar(), { pane: pane(f), row: rowState() });
    out.ownAgain = (await api('POST', '/api/stream', { url: 'http://fake.invalid/hang?for=own' })).status;
    await until(() => !bar().failed.shown && bar().label, 8000);
    out.ownCleared = bar();
    errors.push((win().__errors || []).slice());
    out.errors = errors;
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);
});
