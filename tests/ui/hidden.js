// A hidden tab keeps polling, slower (every 5s: background notifications and the title need it), and
// polls again the moment it is shown.
__t.run(async () => {
    const { sleep } = __t;
    const t0 = Date.now(), polls = [];
    const open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
        polls.push([Date.now() - t0, String(url)]);
        return open.apply(this, arguments);
    };
    await sleep(1500);
    __t.hide(true);
    const hiddenAt = Date.now() - t0;
    await sleep(11500);
    const shownAt = Date.now() - t0;    // before the show: the poll it triggers is stamped within the same millisecond, or later
    __t.hide(false);
    await sleep(2500);
    await __t.post({ hiddenAt, shownAt, polls, errors: window.__errors });
});
