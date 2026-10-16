// The live stream's full screen: the button in the pane's corner (only over a picture), the pane covering the whole
// window in portrait and in landscape (the board behind it, inert) and back to the half it had, Escape, a rotation
// while full; this page's own reconnect keeps it; the board's run ending drops it at once and the restarted run opens
// in the split; a restart between two polls (the same stream live again from a later since) and a closed stream drop
// it too; it is this page's alone (another board page beside it stays split, nothing is stored, a reload starts split);
// no button over an audio-only stream. The boards are iframes of a /__frame page, with the media stub.
__t.run(async () => {
    const { sleep, until, pane, size, api, frame } = __t;
    const out = { post: {} };
    const opts = { __media: 'stub', __autoplay: 'allowed' };
    const open = async (url) => (await api('POST', '/api/stream', { url })).status;
    const video = (f) => until(() => pane(f).state === 'video', 12000);
    const look = (f) => {
        const d = f.contentDocument, btn = d.getElementById('stream-full'), w = f.contentWindow;
        let stored = '';
        try { stored = (w.localStorage.getItem('taskstatus.settings.local') || '') + '|' + JSON.stringify(Object.keys(w.sessionStorage)); } catch (e) { stored = 'bad'; }
        return Object.assign(pane(f), {
            full: d.documentElement.classList.contains('stream-full'), inert: d.getElementById('board').inert === true,
            btn: btn.hidden ? null : { pressed: btn.getAttribute('aria-pressed'), label: btn.getAttribute('aria-label'), title: btn.title,
                                       rect: __t.rect(btn), opacity: w.getComputedStyle(btn).opacity },
            // what a tap in the board's top left corner would reach: the board, or the pane over it
            topLeft: (() => { const el = d.elementFromPoint(20, 20); return el ? (el.closest('#stream') ? 'pane' : el.closest('#board') ? 'board' : el.tagName) : null; })(),
            stored,
        });
    };
    const click = async (f) => {
        f.contentDocument.getElementById('stream-full').click();
        await sleep(300);
        return look(f);
    };

    // A picture in portrait: the button over it, the split
    const f = await frame('/', 390, 844, opts);
    out.post.live = await open('http://fake.invalid/live?w=1920&h=1080');
    await video(f);
    await sleep(400);
    out.splitPortrait = look(f);
    out.fullPortrait = await click(f);
    out.fullPortrait.videoRect = __t.rect(f.contentDocument.querySelector('#stream video'));
    await size(f, 844, 390);
    out.fullRotated = look(f);
    out.splitLandscape = await click(f);
    out.fullLandscape = await click(f);
    f.contentWindow.dispatchEvent(new f.contentWindow.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await sleep(300);
    out.escaped = look(f);
    await size(f, 390, 844);

    // this page's own reconnect (the browser failed to decode): the board's run goes on, so full screen stays
    await click(f);
    f.contentWindow.__rec.media.fail('decode error');
    await until(() => pane(f).state === 'connecting', 3000);
    out.ownRetry = look(f);
    await video(f);
    await sleep(300);
    out.ownRetried = look(f);

    // another board page beside this one (another tab) has its own: split, while this one is full
    const g = await frame('/', 390, 844, opts);
    await video(g);
    out.other = look(g);
    __t.drop(g);
    // nothing about it is stored: a reload starts in the split
    out.storedFull = look(f).stored;
    const reloaded = new Promise((resolve) => f.addEventListener('load', resolve, { once: true }));
    f.contentWindow.location.reload();
    await reloaded;
    await until(() => f.contentDocument.getElementById('conn').dataset.state === 'live', 8000);
    await video(f);
    await sleep(300);
    out.reloaded = look(f);

    // The board's run ends (the source stops after 40 fragments, 8s): full screen goes as soon as the page hears of it,
    // and the run the board starts again opens in the split
    out.post.die = await open('http://fake.invalid/live?w=1920&h=1080&die=40');
    await until(() => pane(f).state === 'connecting', 4000);
    await video(f);
    await sleep(300);
    out.dieFull = await click(f);
    await until(() => !look(f).full, 15000);
    out.dieEnded = look(f);
    await until(() => (pane(f).state === 'video' && f.contentDocument.getElementById('stream').dataset.state === 'video'), 15000);
    await sleep(300);
    out.dieRestarted = look(f);
    await api('DELETE', '/api/stream');

    // A restart between two polls: the same stream live again from a later since, never reported as connecting
    await fetch('/__stream?url=' + encodeURIComponent('http://fake.invalid/live?w=1920&h=1080') + '&state=live&id=feedfacecafe&ago=30');
    await video(f);
    await sleep(300);
    out.runFull = await click(f);
    await fetch('/__stream?url=' + encodeURIComponent('http://fake.invalid/live?w=1920&h=1080') + '&state=live&id=feedfacecafe&ago=0');
    await until(() => !look(f).full, 5000);
    out.runNew = look(f);
    // a stream closed while full: no pane, the board has the whole window again, not inert
    await video(f);
    await sleep(300);
    out.closeFull = await click(f);
    await fetch('/__stream?off=1');
    await until(() => !pane(f).streaming, 5000);
    out.closed = look(f);

    // audio only: no button
    out.post.audio = await open('http://fake.invalid/audio');
    await until(() => pane(f).state === 'audio', 12000);
    await sleep(300);
    out.audio = look(f);

    out.errors = [(f.contentWindow.__errors || []).slice()];
    await __t.cleanup();
    await __t.post(out);
});
