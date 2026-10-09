// The player against the stub MediaSource (the fake ffmpeg's fragments, a keyframe every two seconds of media time):
//  - a board that joins a running stream starts inside what the first append buffered, not at 0 (its GOP does not
//    begin there), and does not seek again while it plays steadily;
//  - a playhead that falls behind (the stub holds it) is brought to the live edge with one jump, never two within two
//    seconds; a playhead that does not move because the video is paused (the browser refused it) is never chased,
//    nor one in a hidden tab, until the tab is shown;
//  - buffered media far behind the playhead is trimmed, up to the playhead minus a margin larger than a GOP; the same
//    trim with no margin would cut the playing GOP, which the stub (its remove() runs on to the next keyframe, as a
//    browser's does) shows: the check that the stub can tell.
// The trim thresholds are shortened for the case (harness __tune); the margin keeps its proportion to the GOP.
__t.run(async () => {
    const { sleep, until, pane, api, hide } = __t;
    const out = { T0: Date.now() };
    const media = (f) => f.contentWindow.__rec.media;
    const click = (f) => f.contentDocument.body.dispatchEvent(new f.contentWindow.MouseEvent('click', { bubbles: true }));
    out.post = (await api('POST', '/api/stream', { url: 'http://fake.invalid/live' })).status;
    await sleep(3500);   // the stream has run for a while: the GOP a new board gets starts late in media time

    const f = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed' });
    const p = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'blocked' });
    const t = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed', __tune: 'STREAM_TRIM_S:6,STREAM_KEEP_S:3' });
    const bad = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'allowed', __tune: 'STREAM_TRIM_S:6,STREAM_KEEP_S:0' });
    await until(() => pane(f).state === 'video' && pane(t).state === 'video' && pane(bad).state === 'video' && pane(p).held, 12000);

    // steady playback: the one first seek, and where it landed
    await sleep(3000);
    const m = media(f);
    out.first = { seeks: m.seeks.map((s) => ({ from: s.from, to: s.to, buffered: s.buffered, paused: s.paused })), snap: m.snap() };

    // a held playhead: one jump to the live edge, then none for two seconds
    m.hold = true;
    await until(() => m.seeks.length >= 2, 5000, 50);
    out.jump = { seeks: m.seeks.map((s) => ({ at: s.at, from: s.from, to: s.to, buffered: s.buffered })), snap: m.snap() };
    await sleep(6000);
    out.chase = { seeks: m.seeks.map((s) => s.at), snap: m.snap() };
    m.hold = false;
    await sleep(2500);   // at most one more jump, if the playhead was left more than 1.5s behind
    const before = m.seeks.length;
    await sleep(3000);
    out.steady = { before, after: m.seeks.length, snap: m.snap() };

    // paused (the browser refused playback): the buffer grows, the playhead is left alone; a click starts it, and it jumps
    const mp = media(p);
    out.paused = { seeks: mp.seeks.length, snap: mp.snap(), held: pane(p).held };
    p.contentWindow.__rec.gesture = true;
    click(p);
    await until(() => mp.seeks.length >= 2, 4000, 50);
    out.pausedClicked = { seeks: mp.seeks.map((s) => ({ from: s.from, to: s.to, buffered: s.buffered, paused: s.paused })), snap: mp.snap(), held: pane(p).held };

    // hidden: no chase while hidden, one when shown
    m.hold = true;
    hide(true, f.contentDocument);
    const hiddenFrom = m.seeks.length;
    await sleep(3500);
    out.hidden = { from: hiddenFrom, to: m.seeks.length, snap: m.snap() };
    hide(false, f.contentDocument);
    await until(() => m.seeks.length > hiddenFrom, 3000, 50);
    out.shown = { seeks: m.seeks.map((s) => ({ from: s.from, to: s.to, buffered: s.buffered, hidden: s.hidden })), snap: m.snap() };
    m.hold = false;

    // the trim: wait until both tuned boards have trimmed (about ten seconds of playing)
    await until(() => media(t).removes.length >= 1 && media(bad).removes.length >= 1, 14000, 100);
    await sleep(1500);
    const describe = (g) => ({ removes: media(g).removes, snap: media(g).snap(), srcs: media(g).srcs.length, state: pane(g).state });
    out.trim = describe(t);
    out.cut = describe(bad);
    out.errors = [f, p, t, bad].map((g) => g.contentWindow.__errors.slice());
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);
});
