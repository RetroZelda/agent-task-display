// The board goes out of reach for five seconds while a stream plays, and the stream is replaced meanwhile (the API call
// of the test carries X-Test, so it gets through): the page keeps playing what it has until the old media ends, its
// retries meanwhile fail quietly and slowly (the media request is dropped like the polls), and when the board is back
// the page takes the new stream, from the new id, without a reload and without an error.
__t.run(async () => {
    const { sleep, until, pane, api } = __t;
    const out = { T0: Date.now() };
    const t0 = Date.now();
    const open = async (url) => (await api('POST', '/api/stream', { url })).data.stream.id;
    out.a = await open('http://fake.invalid/live?marker=A');
    const f = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed' });
    const media = f.contentWindow.__rec.media;
    await until(() => pane(f).state === 'video', 12000);
    await sleep(800);
    out.playing = pane(f);
    await fetch('/__offline?ms=5000');
    const off0 = Date.now();
    await sleep(500);
    out.b = await open('http://fake.invalid/live?marker=B');
    out.samples = [];
    let last = '';
    while (Date.now() - off0 < 26000) {
        const p = pane(f), conn = f.contentDocument.getElementById('conn').dataset.state, key = p.state + '|' + p.title + '|' + conn;
        if (key !== last) { last = key; out.samples.push({ at: Date.now() - off0, state: p.state, title: p.title, conn, videos: p.videos }); }
        if (p.state === 'video' && conn === 'live' && media.fragments.length && media.fragments[media.fragments.length - 1].marker === 'B') break;
        await sleep(100);
    }
    await sleep(1200);
    out.final = pane(f);
    out.markers = [...new Set(media.fragments.map((x) => x.marker))].join('');
    out.last = media.fragments.slice(-3).map((x) => x.marker).join('');
    out.fetches = media.fetches.map((x) => [x[0] - off0, x[1].split('id=')[1] === out.a ? 'A' : 'B', x[2]]);
    out.viewers = (await api('GET', '/api/stream')).data.stream.viewers;
    out.errors = [f.contentWindow.__errors.slice()];
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);
});
