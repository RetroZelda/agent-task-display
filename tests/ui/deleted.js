// Detail view: the request is deleted behind the page's back; the next poll shows the deleted view.
// Its own polls stop, but the alerts' go on (this may be the tab kept for them): a question asked on
// another request afterwards still notifies and chimes (stubbed, __stub=1).
__t.run(async () => {
    const { sleep, $, api } = __t;
    await sleep(800);
    const res = await fetch('/api/requests/' + location.pathname.split('/')[2], { method: 'DELETE' });
    await sleep(2700);
    const polls = [];
    const open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) { polls.push(String(url)); return open.apply(this, arguments); };
    const view = $('view-notfound').hidden ? 'other' : 'notfound';
    const other = await api('POST', '/api/requests', { title: 'Asked after the deletion', tasks: ['a step'] });
    const oid = other.data && other.data.id;
    await api('POST', '/api/requests/' + oid + '/attention', { message: 'Still there?' });
    await sleep(4000);
    const result = {
        deleteStatus: res.status, view, viewAfter: $('view-notfound').hidden ? 'other' : 'notfound',
        nfTitle: $('nf-title').textContent, nfText: $('nf-text').textContent, title: document.title, polls,
        notes: __rec.notes.map((n) => n.title), chimes: __t.chimes().map((c) => c.kind),
        favicon: decodeURIComponent($('favicon').getAttribute('href')), errors: window.__errors,
    };
    await api('DELETE', '/api/requests/' + oid);  // leave the shared board as it was
    await __t.post(result);
});
