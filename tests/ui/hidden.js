// A hidden tab stops polling and polls again the moment it is shown.
__t.run(async () => {
    const { sleep } = __t;
    const t0 = Date.now(), polls = [];
    const open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
        polls.push([Date.now() - t0, String(url)]);
        return open.apply(this, arguments);
    };
    let hidden = false;
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
    const flip = (value) => { hidden = value; document.dispatchEvent(new Event('visibilitychange')); return Date.now() - t0; };
    await sleep(1000);
    const hiddenAt = flip(true);
    await sleep(4000);
    const shownAt = flip(false);
    await sleep(2500);
    await __t.post({ hiddenAt, shownAt, polls, errors: window.__errors });
});
