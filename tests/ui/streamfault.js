// What the player does when something goes wrong, against the stub MediaSource, one fault at a time; each ends in the
// pane saying why (its tooltip) and the page asking again after its backoff, except a browser that cannot play at all:
//  - the browser's buffer is full (QuotaExceededError on an append): reconnect
//  - the MediaSource never opens (shortened wait): reconnect, again and again, slower each time
//  - no MediaSource at all (neither kind): "cannot play live streams", and not one request for the media
//  - the relay goes silent (fake `stall`; the page's limit shortened): reconnect
//  - bytes arrive but the buffered end stands still, because the audio stopped (fake `drop-audio`, the page's limit
//    shortened to beat the board's own watchdog): reconnect
//  - ManagedMediaSource (Safari): it opens only for an element with disableRemotePlayback; endstreaming makes the page
//    stop reading, startstreaming makes it read again, and the element is not torn down meanwhile
__t.run(async () => {
    const { sleep, until, pane, api, drop } = __t;
    const out = { T0: Date.now() };
    const media = (f) => f.contentWindow.__rec.media;
    const open = async (url) => (await api('POST', '/api/stream', { url })).data.stream;
    const titles = async (f, ms, stop) => {          // every distinct state|tooltip the pane shows for ms
        const seen = [], end = Date.now() + ms;
        while (Date.now() < end) {
            const p = pane(f), key = p.state + '|' + (p.title || '');
            if (seen[seen.length - 1] !== key) seen.push(key);
            if (stop && stop(p)) break;
            await sleep(40);
        }
        return seen;
    };

    // 1. a live stream: a full buffer, a MediaSource that does not open, no MediaSource
    await open('http://fake.invalid/live');
    const q = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed' });
    const o = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed', __open: 'never', __tune: 'STREAM_OPEN_MS:1000' });
    const n = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed', __mse: 'none' });
    await until(() => pane(q).state === 'video', 12000);
    await sleep(1000);
    const t0 = Date.now();
    media(q).failNext = 'quota';
    out.quota = { seen: await titles(q, 6000, (p) => p.state === 'video' && media(q).srcs.length >= 2), srcs: media(q).srcs.length, loads: media(q).loads,
                  fetches: media(q).fetches.map((x) => x[0] - t0), after: pane(q) };
    await sleep(2500);
    out.open = { seen: await titles(o, 100), srcs: media(o).srcs.length, opens: media(o).opens, fetches: media(o).fetches.map((x) => x[0]), pane: pane(o) };
    out.noMse = { pane: pane(n), fetches: media(n).fetches.length, srcs: media(n).srcs.length };
    drop(n);
    drop(o);
    drop(q);

    // 2. the relay goes silent after three fragments
    await open('http://fake.invalid/stall?n=3');
    const s = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed', __tune: 'STREAM_STALL_MS:2500' });
    out.stall = { seen: await titles(s, 9000, (p) => /no data/.test(p.title || '') && media(s).srcs.length >= 2), srcs: media(s).srcs.length, fetches: media(s).fetches.length };
    drop(s);

    // 3. the audio stops after five seconds; the video goes on
    await open('http://fake.invalid/drop-audio?n=25');
    const d = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed', __tune: 'STREAM_FROZEN_MS:1000' });
    await until(() => pane(d).state === 'video', 12000);
    out.frozen = { seen: await titles(d, 14000, (p) => /froze/.test(p.title || '')), srcs: media(d).srcs.length };
    out.frozenLater = { seen: await titles(d, 25000, (p) => p.state === 'video' && media(d).srcs.length >= 2), server: (await api('GET', '/api/stream')).data.stream };
    drop(d);

    // 4. ManagedMediaSource only (a Safari)
    await open('http://fake.invalid/live');
    const m = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed', __mse: 'mms' });
    await until(() => pane(m).state === 'video', 12000);
    await sleep(1500);
    out.mms = { pane: pane(m), opens: media(m).opens, srcs: media(m).srcs.length, fetches: media(m).fetches.length, viewers: (await api('GET', '/api/stream')).data.stream.viewers };
    media(m).endStreaming();
    await sleep(2500);
    const ended = media(m).snap();
    out.mmsEnded = { fetches: media(m).fetches.length, viewers: (await api('GET', '/api/stream')).data.stream.viewers, loads: media(m).loads, state: pane(m).state, snap: ended };
    media(m).startStreaming();
    await until(() => media(m).fetches.length >= 2, 4000, 50);
    await sleep(2500);
    out.mmsResumed = { fetches: media(m).fetches.length, viewers: (await api('GET', '/api/stream')).data.stream.viewers, loads: media(m).loads, state: pane(m).state,
                       snap: media(m).snap(), seeks: media(m).seeks.length, inits: media(m).inits };

    out.errors = [m.contentWindow.__errors.slice()];
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);
});
