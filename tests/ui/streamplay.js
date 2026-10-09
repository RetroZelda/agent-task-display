// Real playback: the board's own ffmpeg remuxes a real H.264 + AAC source (a generated MPEG-TS), and this Firefox decodes
// it through its own MediaSource, no stub. First a preflight that needs no board code but the media route: this page
// makes a MediaSource of its own, appends a few seconds of what the board relays, and waits for loadeddata: if Firefox
// cannot decode that (no H.264 here, say), the case says so and stops. Then a board in a frame plays the same stream:
// the video has the source's size and moves, the buffer stays bounded, and the sound is held back (no gesture yet).
__t.run(async () => {
    const { sleep, until, pane, api } = __t;
    const out = { T0: Date.now() };
    const stream = (await api('POST', '/api/stream', { url: __t.params.get('__src') })).data.stream;
    out.post = { id: stream.id, state: stream.state };
    for (let i = 0; i < 80; i++) {
        out.server = (await api('GET', '/api/stream')).data.stream;
        if (out.server.state === 'live') break;
        await sleep(250);
    }
    out.live = out.server.state === 'live';
    if (!out.live) { await __t.cleanup(); await __t.post(out); return; }     // the board's own failure: test_ui.py reports it

    // the preflight
    const pre = out.pre = { supported: false, decoded: false };
    const res = await fetch(out.server.media);
    const type = res.headers.get('Content-Type') || '';
    pre.type = type;
    pre.supported = typeof MediaSource === 'function' && MediaSource.isTypeSupported(type);
    if (pre.supported) {
        const v = document.createElement('video');
        v.muted = true;
        v.style.cssText = 'position:absolute;left:-400px;top:0;width:320px;height:180px';
        document.body.appendChild(v);
        const ms = new MediaSource();
        v.src = URL.createObjectURL(ms);
        await new Promise((resolve) => ms.addEventListener('sourceopen', resolve, { once: true }));
        const sb = ms.addSourceBuffer(type);
        const reader = res.body.getReader();
        let loaded = false;
        v.addEventListener('loadeddata', () => { loaded = true; });
        const t0 = Date.now();
        while (Date.now() - t0 < 8000 && !loaded) {
            const r = await reader.read();
            if (r.done) break;
            await new Promise((resolve, reject) => { sb.addEventListener('updateend', resolve, { once: true }); sb.addEventListener('error', reject, { once: true }); sb.appendBuffer(r.value); });
            if (sb.buffered.length && v.currentTime < sb.buffered.start(0)) v.currentTime = sb.buffered.start(0);
            await sleep(50);
        }
        await until(() => loaded, 2500, 50);
        pre.decoded = loaded && v.videoWidth > 0;
        pre.size = [v.videoWidth, v.videoHeight];
        pre.error = v.error && v.error.message;
        reader.cancel().catch(() => {});
        v.removeAttribute('src');
        v.load();
        v.remove();
    }
    if (!pre.decoded) { out.skip = pre.supported ? 'this Firefox did not decode the relayed H.264 (readyState/loadeddata never came)' : 'this Firefox has no MediaSource for ' + type; await __t.cleanup(); await __t.post(out); return; }

    // the board's page, with the real player
    const f = await __t.frame('/', 390, 844, {});
    await until(() => pane(f).state === 'video', 20000, 100);
    await sleep(1000);
    const v = () => f.contentDocument.querySelector('#stream video');
    const quality = () => { const q = v().getVideoPlaybackQuality(); return q.totalVideoFrames - q.droppedVideoFrames; };
    out.pane = pane(f);
    const a = { t: v().currentTime, frames: quality(), at: Date.now() };
    await sleep(3000);
    const b = { t: v().currentTime, frames: quality(), at: Date.now() };
    const buffered = v().buffered;
    out.play = { w: v().videoWidth, h: v().videoHeight, readyState: v().readyState, paused: v().paused, muted: v().muted, error: v().error && v().error.message,
                 advanced: b.t - a.t, frames: b.frames - a.frames, seconds: (b.at - a.at) / 1000, ranges: buffered.length,
                 span: buffered.length ? buffered.end(buffered.length - 1) - buffered.start(0) : 0,
                 lag: buffered.length ? buffered.end(buffered.length - 1) - v().currentTime : null };
    out.errors = [f.contentWindow.__errors.slice()];
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);
});
