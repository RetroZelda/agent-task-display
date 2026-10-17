// The finished requests a page at a time, on a board of the case's own (37 finished, 2 running). The board page is an
// iframe of a /__frame page, so it can be reloaded and can go to a request and Back. Each step reads the pager and the
// finished rows, and the page of the API they should be (asked by the test with the same limit and offset): every
// running request on every page; first, previous, next, last; a page size of 20 (stored on the device, keeping the
// first request shown on the page shown); a reload and Back from a request keeping the page; a request finishing
// (the pages move along); requests deleted under a page (it moves to the last one there is); a short list (no pager).
__t.run(async () => {
    const { sleep, until, api, frame } = __t;
    const ids = (d) => [...d.querySelectorAll('#list-finished > .row')].map((a) => a.getAttribute('href').slice(3));
    const want = async (limit, offset) => (await api('GET', `/api/requests?limit=${limit}&offset=${offset}`)).data.requests
        .filter((r) => r.status !== 'running').map((r) => r.id);
    const look = (f) => {
        const d = f.contentDocument, w = f.contentWindow, $ = (id) => d.getElementById(id);
        let stored = null;
        try { stored = w.localStorage.getItem('taskstatus.finishedPageSize'); } catch (e) { stored = 'bad'; }
        return { info: $('page-info').textContent, pager: !$('pager').hidden, count: $('count-finished').textContent, size: $('page-size').value,
                 disabled: ['page-first', 'page-prev', 'page-next', 'page-last'].map((id) => $(id).disabled), rows: ids(d),
                 running: d.querySelectorAll('#list-active > .row').length, kept: w.history.state ? w.history.state.finishedPage ?? null : null,
                 stored, scroll: [d.documentElement.scrollWidth, d.documentElement.clientWidth], path: w.location.pathname };
    };
    // until the rows are the API's page (limit, offset) and the pager says which; the step with what was wanted
    const settle = async (f, limit, offset, info) => {
        const page = await want(limit, offset);
        await until(() => JSON.stringify(ids(f.contentDocument)) === JSON.stringify(page) && f.contentDocument.getElementById('page-info').textContent === info, 6000);
        return Object.assign(look(f), { want: page });
    };
    const click = (f, id) => f.contentDocument.getElementById(id).click();
    // the page size, picked in the document the frame holds now (a reload or Back makes another)
    const pick = (f, n) => {
        const sel = f.contentDocument.getElementById('page-size');
        sel.value = String(n);
        sel.dispatchEvent(new f.contentWindow.Event('change'));
    };
    const out = {};
    const f = await frame('/', 390, 844, { __keep: '1' });
    out.first = await settle(f, 10, 0, '1–10 of 37');
    click(f, 'page-next');
    out.second = await settle(f, 10, 10, '11–20 of 37');
    click(f, 'page-last');
    out.last = await settle(f, 10, 30, '31–37 of 37');
    click(f, 'page-prev');
    out.prev = await settle(f, 10, 20, '21–30 of 37');
    click(f, 'page-first');
    out.backFirst = await settle(f, 10, 0, '1–10 of 37');
    click(f, 'page-last');
    await settle(f, 10, 30, '31–37 of 37');
    // 20 to a page: the 31st request was shown first, so the page that holds it, 21-37
    pick(f, 20);
    out.size20 = await settle(f, 20, 20, '21–37 of 37');

    // a reload keeps the page size (this device's) and the page (this history entry's)
    let loaded = new Promise((resolve) => f.addEventListener('load', resolve, { once: true }));
    f.contentWindow.location.reload();
    await loaded;
    out.reloaded = await settle(f, 20, 20, '21–37 of 37');
    // to a request on this page, and Back: the same page
    const target = out.reloaded.rows[3];
    loaded = new Promise((resolve) => f.addEventListener('load', resolve, { once: true }));
    f.contentDocument.querySelector(`a.row[href="/r/${target}"]`).click();
    await loaded;
    await until(() => !f.contentDocument.getElementById('view-detail').hidden, 6000);
    out.detail = { path: f.contentWindow.location.pathname, pager: !f.contentDocument.getElementById('pager').hidden };
    f.contentWindow.history.back();
    await until(() => f.contentWindow.location.pathname === '/', 6000);
    await until(() => f.contentDocument.getElementById('view-list') && !f.contentDocument.getElementById('view-list').hidden, 6000);
    out.back = await settle(f, 20, 20, '21–37 of 37');

    // a running request finishes: it is the newest finished, so every page moves along by one
    const running = (await api('GET', '/api/requests?status=running')).data.requests;
    await api('POST', `/api/requests/${running[0].id}/complete`, { status: 'done' });
    out.finishedOne = await settle(f, 20, 20, '21–38 of 38');
    out.finishedOne.newest = (await want(20, 0))[0] === running[0].id;

    // 10 to a page, on the last (31-38); then 20 finished requests deleted: 18 are left, the page moves to the last one
    pick(f, 10);
    out.size10 = await settle(f, 10, 20, '21–30 of 38');
    click(f, 'page-last');
    await settle(f, 10, 30, '31–38 of 38');
    for (const id of await want(20, 0)) await api('DELETE', `/api/requests/${id}`);
    out.shrunk = await settle(f, 10, 10, '11–18 of 18');
    // 8 left: one page at any size, so no pager; every one of them listed
    for (const id of await want(10, 0)) await api('DELETE', `/api/requests/${id}`);
    await until(() => f.contentDocument.getElementById('pager').hidden, 6000);
    out.short = Object.assign(look(f), { want: await want(10, 0) });
    out.errors = [(f.contentWindow.__errors || []).slice()];
    await __t.cleanup();
    await __t.post(out);
});
