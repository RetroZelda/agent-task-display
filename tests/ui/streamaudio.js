// An audio-only stream: "audio connected" in a black pane of 15% of the window (the height below the board in
// portrait, the width beside it in landscape), at four window sizes; and, where the browser lets only muted playback
// start, the click-for-sound chip, which must fit the narrow pane of a phone in landscape (as its icon alone).
__t.run(async () => {
    const { sleep, until, pane, size, api, drop } = __t;
    const out = { post: {} }, T0 = Date.now();
    out.post.open = (await api('POST', '/api/stream', { url: 'http://fake.invalid/audio' })).status;

    const a = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed' });
    await until(() => pane(a).state === 'audio', 12000);
    await sleep(500);
    out.portrait = pane(a);
    out.type = a.contentWindow.__rec.media.supports[0];
    out.plays = a.contentWindow.__rec.media.plays.map((p) => [p.muted, p.result]);
    await size(a, 844, 390);
    out.landscape = pane(a);
    await size(a, 667, 375);
    out.phone = pane(a);
    await size(a, 1280, 800);
    out.wide = pane(a);
    out.timerShown = a.contentWindow.getComputedStyle(a.contentDocument.getElementById('stream-timer')).display;
    drop(a);

    const h = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'muted' });
    await until(() => pane(h).state === 'audio' && pane(h).held, 12000);
    await sleep(300);
    out.heldPortrait = pane(h);
    await size(h, 844, 390);
    out.heldLandscape = pane(h);
    await size(h, 667, 375);
    out.heldPhone = pane(h);
    await size(h, 1280, 800);
    out.heldWide = pane(h);
    out.errors = [h.contentWindow.__errors.slice()];
    out.took = Date.now() - T0;
    await __t.cleanup();
    await __t.post(out);
});
