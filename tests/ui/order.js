// The task order on a request's page: by number, or by progress (running, waiting first; then queued; then done and
// failed; then cancelled; by number within each). The toggle on the Tasks heading, Settings > This device > Task order
// (the board's, or this device's own), the board's setting from the All viewers tab and from behind the page, another
// tab changing it, the order following the tasks as they change, and a board too old to know the setting. Each step
// reads the order of the rows as task numbers. The board is the case's own (test_ui.py seeds the request).
__t.run(async () => {
    const { sleep, $, api, until } = __t;
    const KEY = 'taskstatus.settings.local';
    const rid = location.pathname.split('/').pop();
    const seqs = () => [...document.querySelectorAll('#task-list > .row')].map((el) => Number(el.id.split('-')[1]));
    const stored = () => {
        try { return (JSON.parse(localStorage.getItem(KEY) || 'null') || {}).task_order ?? null; } catch (e) { return 'bad'; }
    };
    const look = (extra) => {
        const dev = document.querySelector('input[name=dev-order]:checked'), glob = document.querySelector('input[name=glob-order]:checked');
        return Object.assign({
            seqs: seqs(), stored: stored(), toggleHidden: $('task-order').hidden,
            pressed: [...$('task-order').children].filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.dataset.order),
            devRadio: dev ? dev.value : null, devNote: $('dev-order-note').textContent,
            globRadio: glob ? glob.value : null, globDisabled: [...document.querySelectorAll('input[name=glob-order]')].map((r) => r.disabled),
            globNote: $('glob-order-note').textContent,
        }, extra || {});
    };
    // Waits for the rows to come in the order wanted (a poll, or the settings reload after one), at most ms.
    const settle = async (want, ms = 5000) => {
        await until(() => JSON.stringify(seqs()) === JSON.stringify(want), ms);
        return look();
    };
    const NUMBER = [1, 2, 3, 4, 5, 6, 7, 8, 9];
    const PROGRESS = [6, 3, 8, 4, 7, 1, 5, 9, 2];
    const out = {};
    await api('DELETE', '/api/settings');
    await until(() => seqs().length === 9, 5000);
    await sleep(400);
    const rows0 = new Map([...document.querySelectorAll('#task-list > .row')].map((el) => [el.id, el]));
    out.initial = look();
    const head = document.querySelector('#view-detail h2.section');
    out.layout = { head: __t.rect(head), toggle: __t.rect($('task-order')), list: __t.rect($('task-list')),
                   buttons: [...$('task-order').children].map((b) => __t.rect(b)), labels: [...$('task-order').children].map((b) => b.textContent),
                   scroll: [document.documentElement.scrollWidth, document.documentElement.clientWidth] };

    // the toggle: this device's own order
    $('task-order').querySelector('[data-order=progress]').click();
    out.toggleProgress = await settle(PROGRESS, 1000);
    out.sameRows = [...document.querySelectorAll('#task-list > .row')].every((el) => rows0.get(el.id) === el);
    $('task-order').querySelector('[data-order=number]').click();
    out.toggleNumber = await settle(NUMBER, 1000);
    $('task-order').querySelector('[data-order=progress]').click();
    await settle(PROGRESS, 1000);

    // Settings > This device: the radio shows the toggle's choice; The board's goes back to the board's (by number)
    $('settings-btn').click();
    await sleep(500);
    out.devProgress = look();
    document.querySelector('input[name=dev-order][value=board]').click();
    out.devBoard = await settle(NUMBER, 1000);

    // Settings > All viewers: By progress, saved on the board; this device follows it
    $('tab-global').click();
    await sleep(800);
    out.globLoaded = look();
    document.querySelector('input[name=glob-order][value=progress]').click();
    await sleep(100);
    out.globDirty = look({ status: $('glob-status').textContent, save: !$('glob-save').disabled });
    $('glob-save').click();
    await sleep(1200);
    const s1 = await api('GET', '/api/settings');
    out.globSaved = Object.assign(await settle(PROGRESS, 1000), { server: s1.data.settings.tasks, status: $('glob-status').textContent });
    $('tab-device').click();
    await sleep(200);
    out.devFollows = look();
    // this device's own By number, whatever the board says
    document.querySelector('input[name=dev-order][value=number]').click();
    out.devNumber = await settle(NUMBER, 1000);

    // another tab of this browser (a same-origin frame) changes the choice
    const frame = document.createElement('iframe');
    frame.style.display = 'none';
    document.body.appendChild(frame);
    const other = frame.contentWindow.localStorage;
    const write = async (order, want) => {
        const s = JSON.parse(other.getItem(KEY) || 'null') || {};
        if (order === undefined) delete s.task_order;
        else s.task_order = order;
        other.setItem(KEY, JSON.stringify(s));
        return settle(want, 2000);
    };
    out.otherBoard = await write(undefined, PROGRESS);      // the board's again: by progress now
    // the board's setting changes behind the page (a hand edit, another viewer): the page follows on its next polls
    await api('PUT', '/api/settings', { settings: { tasks: { order: 'number' } } });
    out.boardNumber = await settle(NUMBER, 6000);
    out.otherProgress = await write('progress', PROGRESS);
    out.otherJunk = await write('random', NUMBER);           // an unknown value: the board's
    frame.remove();

    // the order follows the tasks as they change (this device by progress)
    document.querySelector('input[name=dev-order][value=progress]').click();
    await settle(PROGRESS, 1000);
    await api('POST', `/api/tasks/${rid}-4/progress`, { message: 'started', percent: 10 });
    out.started = await settle([6, 3, 4, 8, 7, 1, 5, 9, 2]);
    await api('POST', `/api/tasks/${rid}-7/attention`, { message: 'which one?' });
    out.asked = await settle([6, 7, 3, 4, 8, 1, 5, 9, 2]);
    await api('POST', `/api/tasks/${rid}-6/complete`, { status: 'done' });
    out.answered = await settle([7, 3, 4, 8, 1, 5, 6, 9, 2]);
    out.rowsKept = [...document.querySelectorAll('#task-list > .row')].every((el) => rows0.get(el.id) === el);
    // a deep link to a task still finds it where it went
    location.hash = '#' + rid + '-1';
    await sleep(500);
    out.target = [...document.querySelectorAll('#task-list > .row.target')].map((el) => el.id);

    // a board too old to know the setting (its settings have no "tasks"; this page is newer than the running server):
    // the All viewers order is read-only and a save sends only what that board knows
    const realFetch = window.fetch, puts = [];
    window.fetch = async (input, opts) => {
        const url = String(input && input.url || input);
        if (!url.includes('/api/settings')) return realFetch(input, opts);
        if (opts && opts.method === 'PUT' && !(opts.headers || {})['X-Test']) puts.push(JSON.parse(opts.body));   // the page's, not this script's
        const res = await realFetch(input, opts), body = await res.json();
        if (body && body.settings) {
            delete body.settings.tasks;
            delete body.defaults.tasks;
        }
        return new Response(JSON.stringify(body), { status: res.status, headers: res.headers });
    };
    $('settings-close').click();
    await api('PUT', '/api/settings', { settings: { sound: { volume: 0.5 } } });   // a new version: the page reloads them
    await sleep(3500);
    $('settings-btn').click();
    $('tab-global').click();
    await sleep(800);
    out.oldBoard = look();
    const vol = $('glob-volume');
    vol.value = '40';
    vol.dispatchEvent(new Event('input'));
    $('glob-save').click();
    await sleep(1200);
    window.fetch = realFetch;
    out.oldSave = { puts, status: $('glob-status').textContent, server: (await api('GET', '/api/settings')).data.settings };
    $('settings-close').click();
    await api('DELETE', '/api/settings');
    out.errors = window.__errors;
    await __t.post(out);
});
