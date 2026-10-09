// The page's lifecycle with a stream attached (the park time is shortened to 2.5s by the harness' __tune):
//  - a tab hidden and muted lets its media connection go after the park time (the board's viewer count drops) and takes
//    it up again when shown; one with its sound on, kept for listening, does not park
//  - a page the browser keeps for Back (pagehide, persisted) stops its player, and attaches again when shown (pageshow)
//  - the browser coming back online cuts a retry's wait short
__t.run(async () => {
    const { sleep, until, pane, api, drop, hide } = __t;
    const out = { T0: Date.now() };
    const media = (f) => f.contentWindow.__rec.media;
    const viewers = async () => (await api('GET', '/api/stream')).data.stream.viewers;
    await api('POST', '/api/stream', { url: 'http://fake.invalid/live' });
    const tune = { __tune: 'STREAM_PARK_MS:2500,STREAM_OPEN_MS:600' };

    const m = await __t.frame('/', 390, 844, Object.assign({ __media: 'stub', __autoplay: 'muted' }, tune));
    const s = await __t.frame('/', 390, 844, Object.assign({ __media: 'stub', __autoplay: 'allowed' }, tune));
    await until(() => pane(m).state === 'video' && pane(s).state === 'video', 12000);
    await sleep(500);
    out.start = { viewers: await viewers(), m: pane(m).videos, s: pane(s).videos };

    hide(true, m.contentDocument);
    hide(true, s.contentDocument);
    await sleep(1200);
    out.early = { m: pane(m).videos, mLoads: media(m).loads, viewers: await viewers() };
    await sleep(2800);
    for (let i = 0; i < 30 && (await viewers()) !== 1; i++) await sleep(100);
    out.parked = { m: pane(m), mLoads: media(m).loads, s: pane(s).videos, sLoads: media(s).loads, viewers: await viewers(), mFetches: media(m).fetches.length };
    hide(false, m.contentDocument);
    hide(false, s.contentDocument);
    await until(() => pane(m).state === 'video', 8000);
    await sleep(500);
    out.back = { m: pane(m), mSrcs: media(m).srcs.length, mFetches: media(m).fetches.length, s: pane(s).videos, sSrcs: media(s).srcs.length, viewers: await viewers() };

    // a page kept for Back
    const w = s.contentWindow;
    w.dispatchEvent(new w.PageTransitionEvent('pagehide', { persisted: true }));
    await sleep(300);
    out.pagehide = { videos: pane(s).videos, loads: media(s).loads };
    w.dispatchEvent(new w.PageTransitionEvent('pageshow', { persisted: true }));
    await until(() => pane(s).state === 'video' && media(s).srcs.length >= 2, 8000);
    out.pageshow = { state: pane(s).state, srcs: media(s).srcs.length };
    drop(m);
    drop(s);

    // online cuts a retry short: a MediaSource that never opens fails every 0.6s (backoff 1, 2, 4s between tries)
    const o = await __t.frame('/', 390, 844, Object.assign({ __media: 'stub', __autoplay: 'allowed', __open: 'never' }, tune));
    await until(() => media(o).loads >= 2 && media(o).fetches.length === 2, 10000, 20);    // two tries failed: the next one waits 2s
    const atFail = Date.now(), n = media(o).fetches.length;
    await sleep(300);
    o.contentWindow.dispatchEvent(new o.contentWindow.Event('online'));
    await until(() => media(o).fetches.length > n, 3000, 20);
    out.online = { waited: Date.now() - atFail, fetches: media(o).fetches.map((x) => x[0] - atFail) };
    out.errors = [m, s, o].map((g) => g.contentWindow ? g.contentWindow.__errors.slice() : []);
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);
});
