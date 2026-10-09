// A stream the browser cannot play (hev1, which the stub's MediaSource refuses, as many real ones do): the pane stays
// half the window with the timer going, the reason is its tooltip only, nothing tries again (one request for the media,
// none after it), and no video element is made.
__t.run(async () => {
    const { sleep, until, pane, api } = __t;
    const out = { T0: Date.now() };
    await api('GET', '/__log?clear=1');
    out.post = (await api('POST', '/api/stream', { url: 'http://fake.invalid/hevc?audio=0' })).data.stream;
    const f = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed' });
    await until(() => pane(f).title, 12000);
    await sleep(500);
    out.early = pane(f);
    const stream = (await api('GET', '/api/stream')).data.stream;
    out.server = { state: stream.state, video: stream.video, audio: stream.audio };
    const before = (await api('GET', '/__log')).data.filter((x) => x[1].startsWith('/api/stream/media')).length;
    await sleep(9000);
    out.late = pane(f);
    out.requests = [before, (await api('GET', '/__log')).data.filter((x) => x[1].startsWith('/api/stream/media')).length];
    out.supports = f.contentWindow.__rec.media.supports.slice();
    out.srcs = f.contentWindow.__rec.media.srcs.length;
    out.errors = [f.contentWindow.__errors.slice()];
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);
});
