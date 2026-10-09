// A newer URL replaces the stream on every open page: two boards play stream A (its fragments carry the marker A),
// are told of B (which never connects: the pane is "connecting" again with the timer started over, and no video
// element is left), then of C: both play it, from fragments marked C only, and the board counts two viewers. The media
// requests the boards made, by stream id, are in /__log.
__t.run(async () => {
    const { sleep, until, pane, api } = __t;
    const out = { ids: {}, T0: Date.now() };
    const open = async (url) => (await api('POST', '/api/stream', { url })).data.stream.id;
    const media = (f) => f.contentWindow.__rec.media;
    const markers = (f) => [...new Set(media(f).fragments.map((x) => x.marker))].join('');
    await api('GET', '/__log?clear=1');

    out.ids.a = await open('http://fake.invalid/live?marker=A');
    const f = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed' });
    const g = await __t.frame('/', 844, 390, { __media: 'stub', __autoplay: 'allowed' });
    await until(() => pane(f).state === 'video' && pane(g).state === 'video', 12000);
    await sleep(1500);
    out.a = { f: pane(f), g: pane(g), markers: [markers(f), markers(g)], viewers: (await api('GET', '/api/stream')).data.stream.viewers };

    out.ids.b = await open('http://fake.invalid/hang');
    await until(() => pane(f).state === 'connecting' && pane(f).line.includes('/hang'), 8000);
    await sleep(1200);
    out.b = { f: pane(f), g: pane(g), viewers: (await api('GET', '/api/stream')).data.stream.viewers };

    out.ids.c = await open('http://fake.invalid/live?marker=C');
    await until(() => pane(f).state === 'video' && pane(g).state === 'video', 12000);
    await sleep(1500);
    out.c = { f: pane(f), g: pane(g), markers: [markers(f), markers(g)], viewers: (await api('GET', '/api/stream')).data.stream.viewers,
              last: [media(f).fragments.slice(-3).map((x) => x.marker).join(''), media(g).fragments.slice(-3).map((x) => x.marker).join('')],
              srcs: [media(f).srcs.length, media(g).srcs.length], loads: [media(f).loads, media(g).loads] };
    out.log = (await api('GET', '/__log')).data.filter((x) => x[1].startsWith('/api/stream/media')).map((x) => x[1].split('id=')[1]);
    out.errors = [f.contentWindow.__errors.slice(), g.contentWindow.__errors.slice()];
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);
});
