// The board restarting under an open page (the harness' /__boot makes /api/events name another start, boot_id):
//  - the page reloads by itself, so it starts over from the board as it is now: the stream the restart dropped is gone
//  - with the Settings dialog open it waits, and reloads once the dialog closes (nothing typed there is lost meanwhile)
//  - a board that names no start (an older one) never makes it reload
__t.run(async () => {
    const { sleep, until, pane, api } = __t;
    const out = { T0: Date.now() };
    await api('POST', '/api/requests', { title: 'A request across a restart', tasks: ['one'] });
    await api('POST', '/api/stream', { url: 'http://fake.invalid/hang' });
    const f = await __t.frame('/', 390, 700, { __media: 'stub' });
    const w = () => f.contentWindow, doc = () => f.contentDocument;
    const nav = () => { try { return w().performance.getEntriesByType('navigation')[0].type; } catch (e) { return String(e); } };
    const ready = () => until(() => doc() && doc().getElementById('conn') && doc().getElementById('conn').dataset.state === 'live' && doc().querySelector('#list-active .row'), 10000);
    await until(() => pane(f).streaming, 8000);
    w().__mark = 'before';
    out.before = { streaming: pane(f).streaming, nav: nav() };

    // 1. the restart: the stream went with it, and a new boot_id
    await api('DELETE', '/api/stream');
    await api('GET', '/__boot?id=b00000000001');
    out.reloaded = !!(await until(() => w() && w().__mark === undefined, 8000));
    await ready();
    await sleep(800);
    out.after = { nav: nav(), streaming: pane(f).streaming, hidden: pane(f).hidden, rows: doc().querySelectorAll('#list-active .row').length };

    // 2. another restart with the Settings dialog open: no reload until it closes
    w().__mark = 'dialog';
    doc().getElementById('settings-btn').click();
    await until(() => doc().getElementById('settings').open, 4000);
    await api('GET', '/__boot?id=b00000000002');
    await sleep(3500);
    out.waited = { mark: w().__mark || null, open: doc().getElementById('settings').open };
    doc().getElementById('settings-close').click();
    out.closedReload = !!(await until(() => w() && w().__mark === undefined, 8000));
    await ready();

    // 3. a board that names no start: no reload, however long
    w().__mark = 'older';
    await api('GET', '/__boot?id=missing');
    await sleep(3500);
    out.older = { mark: w().__mark || null };
    out.errors = [w().__errors.slice()];
    out.took = Date.now() - out.T0;
    await api('GET', '/__boot?off=1');
    await __t.cleanup();
    await __t.post(out);
});
