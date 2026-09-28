// Detail view: the request is deleted behind the page's back; the next poll shows the deleted view
// and polling stops.
__t.run(async () => {
    const { sleep, $ } = __t;
    await sleep(800);
    const res = await fetch('/api/requests/' + location.pathname.split('/')[2], { method: 'DELETE' });
    await sleep(2700);
    const polls = [];
    const open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) { polls.push(String(url)); return open.apply(this, arguments); };
    await sleep(2000);
    await __t.post({
        deleteStatus: res.status, view: $('view-notfound').hidden ? 'other' : 'notfound',
        nfTitle: $('nf-title').textContent, nfText: $('nf-text').textContent, title: document.title,
        pollsAfter: polls.length, errors: window.__errors,
    });
});
