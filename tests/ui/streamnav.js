// Moving about a board whose window is split with a stream (a hang stream: the pane stays "connecting"):
//  - opening or closing a stream keeps the reading position (the document scroller and the board's own are different
//    elements: the position moves from one to the other and back)
//  - a #task deep link scrolls the task into view inside the board, not the document
//  - going to a request and Back restores the board's scroll position (the browser restores only the document's)
//  - the deleted-request view keeps the pane, whose timer reads the board's clock (events carry it) when the browser's
//    own is an hour off
__t.run(async () => {
    const { sleep, until, pane, api, drop, rect } = __t;
    const out = { T0: Date.now() };
    const open = (url) => api('POST', '/api/stream', { url });
    const loaded = (f) => new Promise((resolve) => f.addEventListener('load', resolve, { once: true }));
    const ready = (f, sel) => until(() => f.contentDocument.getElementById('conn') && f.contentDocument.getElementById('conn').dataset.state === 'live' && f.contentDocument.querySelector(sel), 10000);
    for (let i = 1; i <= 14; i++) await api('POST', '/api/requests', { title: 'Nightly job ' + String(i).padStart(2, '0'), tasks: ['collect', 'render'] });
    const many = (await api('POST', '/api/requests', { title: 'A request with many tasks', tasks: Array.from({ length: 40 }, (_, i) => 'task ' + (i + 1)) })).data;
    const small = (await api('POST', '/api/requests', { title: 'A small request', tasks: ['one'] })).data;
    out.ids = { many: many.id, small: small.id };

    // 1. the reading position, from the document to the board and back
    let f = await __t.frame('/', 390, 500, { __media: 'stub' });
    await ready(f, '#list-active .row');
    const doc = () => f.contentDocument.scrollingElement, board = () => f.contentDocument.getElementById('board');
    f.contentWindow.scrollTo(0, 300);
    await sleep(200);
    out.before = { doc: doc().scrollTop, board: board().scrollTop };
    await open('http://fake.invalid/hang');
    await until(() => pane(f).streaming, 8000);
    await sleep(300);
    out.streaming = { doc: doc().scrollTop, board: board().scrollTop, boardH: board().clientHeight, boardScrollH: board().scrollHeight };
    await api('DELETE', '/api/stream');
    await until(() => !pane(f).streaming, 8000);
    await sleep(300);
    out.closed = { doc: doc().scrollTop, board: board().scrollTop };
    drop(f);

    // 2. a deep link, with the stream open
    await open('http://fake.invalid/hang');
    f = await __t.frame('/r/' + many.id + '#' + many.id + '-40', 390, 500, { __media: 'stub' });
    await ready(f, '#task-list .row');
    await sleep(800);
    const row = f.contentDocument.getElementById(many.id + '-40');
    out.hash = { doc: doc().scrollTop, board: board().scrollTop, boardRect: rect(board()), row: rect(row), target: row.classList.contains('target'), streaming: pane(f).streaming };
    drop(f);

    // 3. to a request and Back
    f = await __t.frame('/', 390, 500, { __media: 'stub' });
    await ready(f, '#list-active .row');
    await until(() => pane(f).streaming, 8000);
    board().scrollTop = 400;
    await sleep(300);
    out.listScroll = board().scrollTop;
    const there = loaded(f);
    f.contentDocument.querySelector('a.row').click();
    await there;
    await ready(f, '#task-list .row');
    out.detail = { path: f.contentWindow.location.pathname, streaming: pane(f).streaming };
    const back = loaded(f);
    f.contentWindow.history.back();
    await back;
    await ready(f, '#list-active .row');
    await sleep(800);
    out.back = { path: f.contentWindow.location.pathname, board: board().scrollTop, doc: doc().scrollTop, streaming: pane(f).streaming, state: history_state(f) };
    drop(f);

    // 4. the deleted-request view: the pane stays, and its timer is right with a clock an hour fast
    f = await __t.frame('/r/' + small.id, 390, 700, { __media: 'stub', __skew: '3600' });
    await ready(f, '#task-list .row');
    await until(() => pane(f).streaming, 8000);
    await api('DELETE', '/api/requests/' + small.id);
    await until(() => !f.contentDocument.getElementById('view-notfound').hidden, 8000);
    await sleep(1500);
    const t1 = pane(f);
    await sleep(2200);
    const t2 = pane(f);
    out.deleted = { title: f.contentDocument.getElementById('nf-title').textContent, first: t1.timer, later: t2.timer, streaming: t2.streaming, line: t2.line };
    // and a request that never existed
    drop(f);
    f = await __t.frame('/r/zzzzzz', 390, 700, { __media: 'stub', __skew: '3600' });
    await until(() => f.contentDocument.getElementById('view-notfound') && !f.contentDocument.getElementById('view-notfound').hidden && pane(f).streaming && pane(f).timer, 8000);
    await sleep(1200);
    out.unknown = { title: f.contentDocument.getElementById('nf-title').textContent, timer: pane(f).timer, streaming: pane(f).streaming };
    out.errors = [f.contentWindow.__errors.slice()];
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);

    function history_state(g) { try { return JSON.stringify(g.contentWindow.history.state); } catch (e) { return String(e); } }
});
