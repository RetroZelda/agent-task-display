// The "Live stream" block of the settings panel (This device): the sound switch and the volume of the stream, kept in
// this browser's localStorage under stream_audio and stream_volume, and followed by every other board of the browser
// through the storage event. Two boards play one stream where only muted playback may start; the panel is opened
// in one. A switch off mutes both; on again, a click lets the sound out of the board it was clicked in, and the
// other one, which got no gesture, holds it back with its chip. The volume reaches both elements.
__t.run(async () => {
    const { sleep, until, pane, api } = __t;
    const out = { T0: Date.now() };
    const media = (f) => f.contentWindow.__rec.media;
    const stored = (f) => JSON.parse(f.contentWindow.localStorage.getItem('taskstatus.settings.local') || '{}');
    out.post = (await api('POST', '/api/stream', { url: 'http://fake.invalid/live' })).status;
    const a = await __t.frame('/', 1000, 700, { __media: 'stub', __autoplay: 'muted' });
    const b = await __t.frame('/', 390, 844, { __media: 'stub', __autoplay: 'muted', __keep: '1' });
    await until(() => pane(a).state === 'video' && pane(b).state === 'video' && pane(a).held && pane(b).held, 12000);
    await sleep(500);
    const $a = (id) => a.contentDocument.getElementById(id);
    out.start = { a: pane(a), b: pane(b), stored: stored(a) };

    $a('settings-btn').click();
    await sleep(400);
    out.open = { dialog: $a('settings').open, title: $a('settings-title').textContent, gear: $a('settings-btn').getAttribute('aria-label'),
                 checked: $a('dev-stream-audio').checked, volume: $a('dev-stream-volume').value, volumeText: $a('dev-stream-volume-val').textContent,
                 heldNote: !$a('dev-stream-held').hidden, heading: [...$a('pane-device').querySelectorAll('h3')].map((h) => h.textContent) };

    // off: both muted, nothing held, saved
    $a('dev-stream-audio').click();
    await sleep(500);
    out.off = { a: pane(a), b: pane(b), stored: stored(a), checkbox: $a('dev-stream-audio').checked, heldNote: !$a('dev-stream-held').hidden,
                sliderDisabled: $a('dev-stream-volume').disabled };

    // on again with a gesture in a: a's sound is out; b, which got none, holds it back
    a.contentWindow.__rec.gesture = true;
    $a('dev-stream-audio').click();
    a.contentWindow.__rec.gesture = false;
    await sleep(600);
    out.on = { a: pane(a), b: pane(b), stored: stored(a), unmutes: [media(a).unmutes.map((u) => u.gesture), media(b).unmutes.length] };

    // the volume: the slider, then the other board follows
    a.contentWindow.__rec.gesture = true;
    const slider = $a('dev-stream-volume');
    slider.value = '40';
    slider.dispatchEvent(new a.contentWindow.Event('input', { bubbles: true }));
    a.contentWindow.__rec.gesture = false;
    await sleep(500);
    out.volume = { a: pane(a), b: pane(b), stored: stored(a), text: $a('dev-stream-volume-val').textContent };

    // a hand edit of the saved settings (a bad volume) is ignored, the defaults stand
    a.contentWindow.localStorage.setItem('taskstatus.settings.local', JSON.stringify({ stream_audio: 'yes', stream_volume: 7 }));
    await sleep(500);
    out.junk = { a: pane(a), b: pane(b) };
    out.errors = [a.contentWindow.__errors.slice(), b.contentWindow.__errors.slice()];
    out.took = Date.now() - out.T0;
    await __t.cleanup();
    await __t.post(out);
});
