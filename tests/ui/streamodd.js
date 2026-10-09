// A board that answers oddly (the harness' /__stream?raw= puts any JSON where the events' "stream" is): an older board
// with no such key, a value that is no object, one without an id, one with only an id and a url, an unknown state with
// markup in its error and a `since` an hour ahead, and a live one whose media path points elsewhere. The page shows
// what it can and breaks nothing: no pane where there is no stream, markup only as text, a timer that never goes
// negative, and its media requests only to this board's own route.
__t.run(async () => {
    const { sleep, until, pane, api } = __t;
    const out = { T0: Date.now() };
    const raw = (value) => api('GET', '/__stream?raw=' + encodeURIComponent(value));
    const f = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed' });
    const settle = async () => { await sleep(1900); return pane(f); };     // a poll or two
    out.start = pane(f);

    await raw('missing');
    out.missing = await settle();
    await raw('"live"');
    out.string = await settle();
    await raw('{"url": "http://odd.invalid/s", "state": "live"}');
    out.noId = await settle();
    await raw('{"id": "abc123", "url": "http://odd.invalid/s"}');
    out.minimal = await settle();
    await raw(JSON.stringify({ id: 'abc124', url: 'http://odd.invalid/s', state: 'bogus', error: '<img src=x onerror=alert(1)>', since: Date.now() / 1000 + 3600 }));
    out.bogus = await settle();
    out.bogus.imgs = f.contentDocument.images.length;
    out.bogus.alerts = f.contentWindow.__alerts.slice();

    await raw(JSON.stringify({ id: 'abc125', url: 'http://odd.invalid/live', state: 'live', video: 'avc1.42C01F', audio: 'mp4a.40.2', width: 320, height: 180,
                               media: '//evil.example/x' }));
    await until(() => pane(f).state === 'video', 12000);
    out.live = pane(f);
    out.fetches = f.contentWindow.__rec.media.fetches.map((x) => x[1]);

    // blocked (the media says hev1), then the same stream with codecs this browser plays: the block lifts
    await raw(JSON.stringify({ id: 'abc126', url: 'http://odd.invalid/h', state: 'live', video: 'hev1', audio: null, width: 320, height: 180 }));
    await until(() => pane(f).title, 8000);
    out.blocked = pane(f);
    await raw(JSON.stringify({ id: 'abc126', url: 'http://odd.invalid/h', state: 'live', video: 'avc1.42C01F', audio: 'mp4a.40.2', width: 320, height: 180 }));
    await until(() => pane(f).state === 'video', 12000);
    out.unblocked = pane(f);

    await raw('missing');
    out.gone = await settle();
    out.errors = [f.contentWindow.__errors.slice()];
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);
});
