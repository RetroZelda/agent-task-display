// Two boards of different shapes (a phone upright and a phone on its side) show the same stream, the same id,
// each laid out for its own window; the board counts both as viewers, and one going away leaves one.
__t.run(async () => {
    const { sleep, until, pane, api, drop } = __t;
    const out = { T0: Date.now() };
    const open = (await api('POST', '/api/stream', { url: 'http://fake.invalid/live?w=1280&h=720' })).data.stream;
    out.id = open.id;
    const f = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed', __vw: '1280', __vh: '720' });
    const g = await __t.frame('/', 844, 390, { __media: 'stub', __autoplay: 'allowed', __vw: '1280', __vh: '720' });
    await until(() => pane(f).state === 'video' && pane(g).state === 'video', 12000);
    await sleep(800);
    out.f = pane(f);
    out.g = pane(g);
    const viewers = async () => (await api('GET', '/api/stream')).data.stream;
    for (let i = 0; i < 40; i++) {
        out.both = await viewers();
        if (out.both.viewers === 2) break;
        await sleep(250);
    }
    out.srcs = [f.contentWindow.__rec.media.srcs.length, g.contentWindow.__rec.media.srcs.length];
    drop(g);
    for (let i = 0; i < 40; i++) {
        out.one = await viewers();
        if (out.one.viewers === 1) break;
        await sleep(250);
    }
    out.errors = [f.contentWindow.__errors.slice()];
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);
});
