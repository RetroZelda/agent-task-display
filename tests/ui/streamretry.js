// A source that ends every few seconds (fake die=30: six seconds of video, then an end of stream and a restart after
// the board's own backoff): the board plays, goes back to "connecting" when the media ends (the server's `since` and
// its reason for the timer and the tooltip), plays again, and so on. The page asks for the media again only once the
// board says it is live again, never twice within a second of itself.
__t.run(async () => {
    const { sleep, until, pane, api } = __t;
    const out = { T0: Date.now(), samples: [] };
    const t0 = Date.now();
    out.post = (await api('POST', '/api/stream', { url: 'http://fake.invalid/live?die=30' })).data.stream;
    const f = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed' });
    let last = '';
    while (Date.now() - t0 < 30000) {
        const p = pane(f), tip = p.title || '';
        const key = p.state + '|' + tip;
        if (key !== last) {
            last = key;
            const stream = (await api('GET', '/api/stream')).data.stream;
            out.samples.push({ at: Date.now() - t0, state: p.state, timer: p.timer, title: p.title, videos: p.videos, server: { state: stream.state, since: stream.since, error: stream.error },
                               now: Date.now() / 1000 });
        }
        await sleep(100);
    }
    out.fetches = f.contentWindow.__rec.media.fetches.map((x) => [x[0] - t0, x[2]]);
    out.srcs = f.contentWindow.__rec.media.srcs.length;
    out.errors = [f.contentWindow.__errors.slice()];
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);
});
