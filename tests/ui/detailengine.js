// The detail view: a task asks, then the request itself, then the task gets its answer; a task
// failure flashes its row; a notification click moves to the task's row.
__t.run(async () => {
    const { sleep, $, api } = __t;
    const rid = __t.params.get('__rid'), tids = __t.params.get('__tids').split(',');
    const T0 = Date.now(), at = () => Date.now() - T0;
    const flashes = [];
    new MutationObserver((recs) => {
        for (const m of recs) {
            if (m.target.classList && m.target.classList.contains('flash')) flashes.push([at(), m.target.id || 'header', m.target.dataset.flash]);
        }
    }).observe($('view-detail'), { attributes: true, attributeFilter: ['class'], subtree: true });
    const snaps = [];
    const snap = (label) => {
        const h = $('req-head'), t = document.getElementById(tids[0]);
        const vis = (el) => (el.hidden ? null : el.textContent);
        snaps.push({
            label, at: at(), title: document.title, hash: location.hash,
            head: { waiting: h.dataset.waiting || null, badge: h.querySelector('.badge').textContent, ask: vis(h.querySelector('[data-ref=ask]')),
                    askQ: h.querySelector('[data-ref=ask]').hidden ? null : h.querySelector('[data-ref=askQ]').textContent,
                    asks: vis(h.querySelector('[data-ref=asks]')), animation: getComputedStyle(h).animationName },
            task: t && { waiting: t.dataset.waiting || null, badge: t.querySelector('.badge').textContent,
                         askQ: t.querySelector('.ask').hidden ? null : t.querySelector('[data-ref=askQ]').textContent,
                         animation: getComputedStyle(t).animationName },
            notes: __rec.notes.length,
        });
    };
    await sleep(2200);
    snap('baseline');
    await api('POST', '/api/tasks/' + tids[0] + '/attention', { message: 'Keep 4K or drop to 1080p?' });
    await sleep(2400);
    snap('a task asks');
    await api('POST', '/api/requests/' + rid + '/attention', { message: 'Also upload it when done?' });
    await sleep(2400);
    snap('the request asks too');
    await api('POST', '/api/tasks/' + tids[0] + '/progress', { message: 'answer received: 1080p', percent: 60 });
    await sleep(2400);
    snap('progress answers the task');
    await api('POST', '/api/tasks/' + tids[1] + '/complete', { status: 'failed', message: 'the second step broke' });
    await sleep(2400);
    snap('a task fails');
    const n = __rec.instances.find((x) => x.tag === 'taskstatus:' + rid + ':task_failed');
    if (n && n.onclick) n.onclick({ preventDefault() {} });
    await sleep(600);
    snap('its notification clicked');
    await __t.post({
        snaps, flashes, errors: window.__errors,
        notes: __rec.notes.map((x) => Object.assign({}, x, { at: x.at - T0 })), closes: __rec.closes.map((c) => [c.at - T0, c.tag]),
        chimes: __t.chimes().map((c) => [c.at - T0, c.kind]), clicked: !!(n && n.onclick),
    });
});
