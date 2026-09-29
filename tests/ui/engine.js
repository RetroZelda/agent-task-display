// The alert engine on the list view, with Notification and AudioContext stubbed (harness __stub=1):
// a question, a failure in a background tab, two chimes close together, a replayed failure, the
// answers, a settings change on the board, a device override, a request going quiet while hidden, and
// the request finishing. Returns what the page did at each step; test_ui.py asserts on it.
__t.run(async () => {
    const { sleep, $, api } = __t;
    const rid = __t.params.get('__rid'), tids = __t.params.get('__tids').split(',');
    const T0 = Date.now(), at = () => Date.now() - T0;
    const steps = [], settingsFetches = [], flashes = [];
    const origFetch = window.fetch;
    window.fetch = function (url, opts) {
        if (String(url).startsWith('/api/settings') && !(opts && opts.headers && opts.headers['X-Test'])) {
            settingsFetches.push([at(), (opts && opts.method) || 'GET']);
        }
        return origFetch.apply(this, arguments);
    };
    const row = () => document.querySelector('a.row[href="/r/' + rid + '"]');
    const snap = (label, extra) => {
        const r = row(), ask = r && r.querySelector('.ask');
        steps.push(Object.assign({
            label, at: at(), title: document.title, summary: [...$('summary').children].filter((e) => !e.hidden).map((e) => e.textContent).join(''),
            firstActive: (document.querySelector('#list-active > .row .title') || {}).textContent || null,
            row: r && { waiting: r.dataset.waiting || null, badge: r.querySelector('.badge').textContent, status: r.dataset.status,
                        ask: ask.hidden ? null : ask.querySelector('[data-ref=askQ]').textContent,
                        askWho: ask.hidden ? null : ask.querySelector('[data-ref=askWho]').textContent,
                        askMore: ask.hidden ? null : ask.querySelector('[data-ref=askMore]').textContent,
                        animation: getComputedStyle(r).animationName },
            favicon: decodeURIComponent($('favicon').getAttribute('href')),
            notes: __rec.notes.length, chimes: __t.chimes().length, calm: document.body.classList.contains('calm'),
        }, extra || {}));
    };
    new MutationObserver((recs) => {
        for (const m of recs) {
            if (m.target.classList && m.target.classList.contains('flash') && m.target.matches('a.row')) {
                flashes.push([at(), m.target.getAttribute('href'), m.target.dataset.flash]);
            }
        }
    }).observe($('list-active'), { attributes: true, attributeFilter: ['class'], subtree: true });
    const titles = async (ms) => {
        const seen = [];
        for (let i = 0; i < ms / 250; i++) {
            await sleep(250);
            if (seen[seen.length - 1] !== document.title) seen.push(document.title);
        }
        return seen;
    };

    await sleep(2500);
    snap('baseline');
    const a = await api('POST', '/api/tasks/' + tids[0] + '/attention', { message: 'Which region should I deploy to?' });
    await sleep(2400);
    snap('A: a task asks', { status: a.status });

    __t.hide(true);
    await sleep(300);
    const b = await api('POST', '/api/tasks/' + tids[1] + '/complete', { status: 'failed', message: 'segfault in the renderer' });
    const hiddenTitles = await titles(8000);
    snap('B: a task fails while hidden', { status: b.status, titles: hiddenTitles });
    __t.hide(false);
    await sleep(700);
    snap('B2: shown again');

    const c1 = await api('POST', '/api/tasks/' + tids[2] + '/complete', { status: 'failed', message: 'second failure' });
    await sleep(1700);
    const c2 = await api('POST', '/api/tasks/' + tids[4] + '/attention', { message: 'Second question?' });
    await sleep(4000);
    snap('C: a failure, then a question soon after', { status: [c1.status, c2.status] });

    const d = await api('POST', '/api/tasks/' + tids[3] + '/complete', { status: 'failed', message: 'failed while the board was away', at: Date.now() / 1000 - 30 },
                        { 'X-Tasks-Replay': '1' });
    await sleep(2600);
    snap('D: a replayed failure', { status: d.status });

    const e1 = await api('DELETE', '/api/tasks/' + tids[0] + '/attention');
    const e2 = await api('DELETE', '/api/tasks/' + tids[4] + '/attention');
    await sleep(2600);
    snap('E: both answered', { status: [e1.status, e2.status] });

    const f = await api('PUT', '/api/settings', { settings: { events: { task_done: { sound: true, notify: true } } } });
    await sleep(2600);
    const f2 = await api('POST', '/api/tasks/' + tids[5] + '/complete', { status: 'done', message: 'zeta finished' });
    await sleep(2600);
    snap('F: the board turns on task_done sound and notify, then a task finishes', { status: [f.status, f2.status] });

    $('settings-btn').click();
    await sleep(300);
    const cell = document.querySelector('#dev-matrix .tri[data-key=task_done][data-channel=sound]');
    const cellStates = [cell.dataset.state];
    cell.click(); cellStates.push(cell.dataset.state);
    cell.click(); cellStates.push(cell.dataset.state);
    $('settings-close').click();
    const localAfter = JSON.parse(localStorage.getItem('taskstatus.settings.local'));
    const chimesBefore = __t.chimes().length;
    const g = await api('POST', '/api/tasks/' + tids[4] + '/complete', { status: 'done', message: 'epsilon finished' });
    await sleep(2600);
    snap('G: this device turns task_done sound off', { status: g.status, cellStates, localAfter, newChimes: __t.chimes().slice(chimesBefore).map((c) => c.kind) });

    __t.hide(true);
    await fetch('/__backdate?rid=' + rid + '&secs=900', { headers: { 'X-Test': '1' } });
    await sleep(7000);
    snap('H: the request goes quiet while hidden');
    __t.hide(false);
    await sleep(700);

    const i = await api('POST', '/api/requests/' + rid + '/complete', { status: 'done', message: 'all good' });
    await sleep(2600);
    snap('I: the request finishes', { status: i.status });

    await __t.post({
        steps, flashes, settingsFetches, errors: window.__errors, alerts: window.__alerts,
        notes: __rec.notes.map((n) => Object.assign({}, n, { at: n.at - T0 })),
        closes: __rec.closes.map((c) => [c.at - T0, c.tag]),
        chimes: __t.chimes().map((c) => [c.at - T0, c.kind]),
    });
});
