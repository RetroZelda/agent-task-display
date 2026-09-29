// The board goes out of reach for ~10s while agents keep writing to it: the title and favicon say so;
// on reconnect one summary notification covers what happened meanwhile, plus the open question, and
// one chime plays.
__t.run(async () => {
    const { sleep, $, api } = __t;
    const tids = __t.params.get('__tids').split(',');
    const T0 = Date.now(), at = () => Date.now() - T0;
    await sleep(2500);
    const snaps = [];
    const snap = (label) => snaps.push({ label, at: at(), conn: $('conn').dataset.state, title: document.title,
                                         favicon: decodeURIComponent($('favicon').getAttribute('href')),
                                         notes: __rec.notes.length, chimes: __t.chimes().length });
    snap('baseline');
    await fetch('/__offline?ms=10000');
    await api('POST', '/api/tasks/' + tids[0] + '/complete', { status: 'failed', message: 'offline failure 1' });
    await api('POST', '/api/tasks/' + tids[1] + '/complete', { status: 'failed', message: 'offline failure 2' });
    await api('POST', '/api/tasks/' + tids[2] + '/complete', { status: 'done' });
    await api('POST', '/api/tasks/' + tids[3] + '/progress', { message: 'moving along', percent: 50 });
    await api('POST', '/api/tasks/' + tids[4] + '/attention', { message: 'Asked while the board was unreachable?' });
    await sleep(7000);
    snap('during the outage');
    for (let i = 0; i < 100 && $('conn').dataset.state !== 'live'; i++) await sleep(250);
    await sleep(2000);
    snap('back');
    await __t.post({ snaps, errors: window.__errors, notes: __rec.notes.map((n) => Object.assign({}, n, { at: n.at - T0 })),
                     chimes: __t.chimes().map((c) => [c.at - T0, c.kind]) });
});
