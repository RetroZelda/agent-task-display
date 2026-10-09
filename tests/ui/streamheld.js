// Sound the browser holds back, on a live stream: where only muted playback may start (the video plays muted, the chip
// says "click for sound"; Escape, a click without the browser's activation and a click with it: only the last lets the
// sound out), where nothing may start (the chip says "click to play"; the click starts it, with sound), a device that
// has the stream's sound off (muted, no chip), and a browser that pauses the video after an unmute it refused (muted
// again, playing again, held again). The stub's play() and unmute follow the policy the board's frame asks for.
__t.run(async () => {
    const { sleep, until, pane, api, drop } = __t;
    const out = { post: {} }, T0 = Date.now();
    out.post.open = (await api('POST', '/api/stream', { url: 'http://fake.invalid/live' })).status;
    const media = (f) => f.contentWindow.__rec.media;
    const click = (f) => f.contentDocument.body.dispatchEvent(new f.contentWindow.MouseEvent('click', { bubbles: true }));
    const key = (f, k) => f.contentDocument.body.dispatchEvent(new f.contentWindow.KeyboardEvent('keydown', { key: k, bubbles: true }));
    const gesture = (f, on) => { f.contentWindow.__rec.gesture = on; };
    const plays = (f) => media(f).plays.map((p) => [p.muted, p.result]);
    const boot = async (autoplay, local, ready) => {
        const f = await __t.frame('/', 390, 844, Object.assign({ __media: 'stub', __autoplay: autoplay }, local ? { __local: JSON.stringify(local) } : {}));
        await until(() => ready(pane(f)), 12000);
        await sleep(400);
        return f;
    };

    // 1. muted playback only: the video plays muted and the sound waits
    let f = await boot('muted', null, (p) => p.state === 'video' && p.held);
    out.muted = { pane: pane(f), plays: plays(f) };
    gesture(f, true);
    key(f, 'Escape');
    await sleep(300);
    out.escape = { pane: pane(f), unmutes: media(f).unmutes.length };
    gesture(f, false);
    click(f);
    key(f, 'a');
    await sleep(300);
    out.noActivation = { pane: pane(f), unmutes: media(f).unmutes.length };
    gesture(f, true);
    click(f);
    await sleep(400);
    out.clicked = { pane: pane(f), plays: plays(f), unmutes: media(f).unmutes.map((u) => [u.gesture, u.paused]) };
    drop(f);

    // 1b. a key (not Escape) does it too
    f = await boot('muted', null, (p) => p.state === 'video' && p.held);
    gesture(f, true);
    key(f, 'a');
    await sleep(400);
    out.keyed = { pane: pane(f), unmutes: media(f).unmutes.length };
    drop(f);

    // 2. nothing may start: the chip says click to play, the click starts it, with sound
    f = await boot('blocked', null, (p) => p.held === 'play');
    out.blocked = { pane: pane(f), plays: plays(f), snap: media(f).snap() };
    gesture(f, true);
    click(f);
    await sleep(500);
    out.blockedClicked = { pane: pane(f), plays: plays(f), snap: media(f).snap() };
    drop(f);

    // 3. this device has the stream's sound off: muted from the start, no chip, a click changes nothing
    f = await boot('muted', { stream_audio: false }, (p) => p.state === 'video');
    out.soundOff = { pane: pane(f), plays: plays(f) };
    gesture(f, true);
    click(f);
    await sleep(300);
    out.soundOffClicked = { pane: pane(f), unmutes: media(f).unmutes.length };
    drop(f);

    // 3b. sound off, and nothing may start: click to play, and it plays muted
    f = await boot('blocked', { stream_audio: false }, (p) => p.held === 'play');
    out.offBlocked = { pane: pane(f), plays: plays(f) };
    gesture(f, true);
    click(f);
    await sleep(500);
    out.offBlockedClicked = { pane: pane(f), plays: plays(f), unmutes: media(f).unmutes.length };
    drop(f);

    // 3c. a stream with no audio track (a camera's): played muted at once, no unmuted try, no chip; and nothing may start: click to play
    await api('POST', '/api/stream', { url: 'http://fake.invalid/live?audio=0' });
    f = await boot('muted', null, (p) => p.state === 'video');
    out.videoOnly = { pane: pane(f), plays: plays(f) };
    drop(f);
    f = await boot('blocked', null, (p) => p.held === 'play');
    out.videoOnlyBlocked = { pane: pane(f), plays: plays(f) };
    gesture(f, true);
    click(f);
    await sleep(500);
    out.videoOnlyClicked = { pane: pane(f), plays: plays(f), unmutes: media(f).unmutes.length };
    drop(f);
    await api('POST', '/api/stream', { url: 'http://fake.invalid/live' });

    // 4. a browser that pauses the video after an unmute it refuses: muted again, playing, held again, once
    f = await boot('muted', null, (p) => p.state === 'video' && p.held);
    media(f).refuseUnmute = true;
    gesture(f, true);
    click(f);
    await sleep(700);
    out.refused = { pane: pane(f), plays: plays(f), unmutes: media(f).unmutes.length, pauses: media(f).pauses.length };
    await sleep(1500);
    out.refusedLater = { unmutes: media(f).unmutes.length, plays: plays(f).length, held: pane(f).held };
    out.errors = [f.contentWindow.__errors.slice()];
    out.took = Date.now() - T0;
    await __t.cleanup();
    await __t.post(out);
});
