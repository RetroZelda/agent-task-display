// List view, keyed reconcile: a progress update arriving by poll updates its row in place. The same
// row elements survive, the row containers see no childList churn, and the running times tick.
__t.run(async () => {
    const { sleep, $ } = __t;
    await sleep(400);
    const rowsOf = () => [...document.querySelectorAll('#list-active > .row, #list-finished > .row')];
    const before = rowsOf();
    let mutations = 0;
    const mo = new MutationObserver((recs) => {
        for (const r of recs) if (r.type === 'childList') mutations += r.addedNodes.length + r.removedNodes.length;
    });
    mo.observe($('list-active'), { childList: true });
    mo.observe($('list-finished'), { childList: true });
    const sample = before.find((r) => r.dataset.status === 'running').querySelector('[data-ref=time]');
    let last = sample.textContent, textChanges = 0;
    const iv = setInterval(() => { if (sample.textContent !== last) { textChanges++; last = sample.textContent; } }, 50);
    const res = await fetch('/api/tasks/' + __t.params.get('__tid') + '/progress', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: 'bumped by the reconcile test', percent: 97, eta_seconds: 30 }),
    });
    await sleep(4000);
    clearInterval(iv);
    const after = rowsOf();
    const bumped = after.find((r) => r.textContent.includes('bumped by the reconcile test'));
    await __t.post({
        post: res.status, rowsBefore: before.length, rowsAfter: after.length,
        sameObjects: after.filter((r) => before.includes(r)).length, mutations, textChanges,
        bumped: !!bumped, bumpedSame: !!bumped && before.includes(bumped),
        bumpedHint: bumped ? bumped.querySelector('[data-ref=hint]').textContent : null,
        title: document.title, favicon: $('favicon').getAttribute('href'), errors: window.__errors,
    });
});
