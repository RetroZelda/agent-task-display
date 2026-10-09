// The live stream pane's layout and its two lines, on a board whose ffmpeg is the fake: no stream (no pane, today's
// layout); a stream that does not connect (a black pane, half the window: below the board in portrait, to its right
// in landscape; "connecting to <url>..." and a timer that survives a reload); the video at 16:9 and at 9:16 in every
// orientation (as big as its aspect ratio asks, at most half); a URL with markup, and one with a password; the stream
// closed (the board has the whole window again, and no media request goes out). The boards are iframes of a /__frame
// page, so each one's window is the size the case chooses.
__t.run(async () => {
    const { sleep, until, pane, size, api } = __t;
    const out = { post: {} }, T0 = Date.now();
    const opts = { __media: 'stub', __autoplay: 'allowed' };
    const open = async (url) => { const r = await api('POST', '/api/stream', { url }); return r.status; };
    const settled = (f, state) => until(() => pane(f).state === state && pane(f).streaming, 12000);
    await api('GET', '/__log?clear=1');

    // No stream: nothing of it is there, and the board has the whole window.
    const f = await __t.frame('/', 390, 844, opts);
    await sleep(1800);
    out.none = pane(f);
    out.noneDoc = { cls: f.contentDocument.documentElement.className + '|' + f.contentDocument.body.className, scrollW: f.contentDocument.documentElement.scrollWidth,
                    clientW: f.contentDocument.documentElement.clientWidth };

    // A stream that never connects: half the window, black, the two lines.
    out.post.hang = await open('http://fake.invalid/hang');
    await settled(f, 'connecting');
    await sleep(1200);
    out.hangPortrait = pane(f);
    const t1 = f.contentDocument.getElementById('stream-timer').textContent;
    await sleep(2300);
    out.hangTimer = [t1, f.contentDocument.getElementById('stream-timer').textContent];
    const doc = f.contentDocument;
    out.hangRoles = [doc.getElementById('stream-line').getAttribute('role'), doc.getElementById('stream-timer').getAttribute('role'),
                     doc.getElementById('stream').getAttribute('aria-label')];
    await size(f, 844, 390);
    out.hangLandscape = pane(f);
    out.hangLandscape.setupWide = f.contentWindow.getComputedStyle(doc.querySelector('.setup .wide')).display;
    out.hangLandscape.docScroll = doc.documentElement.scrollWidth - doc.documentElement.clientWidth;
    out.hangLandscape.boardScroll = doc.getElementById('board').scrollWidth - doc.getElementById('board').clientWidth;
    await sleep(1500);
    // reloaded: the timer goes on from the server's clock, it does not start again
    const reloaded = new Promise((resolve) => f.addEventListener('load', resolve, { once: true }));
    f.contentWindow.location.reload();
    await reloaded;
    await until(() => pane(f).streaming && pane(f).timer, 8000);
    out.reloaded = pane(f);

    // The video, 16:9: the whole width and the height that gives, in portrait; half the width, letterboxed, in landscape.
    out.post.live = await open('http://fake.invalid/live?w=1920&h=1080');
    await settled(f, 'video');
    await sleep(500);
    out.videoLandscape = pane(f);
    out.videoLandscape.fit = f.contentWindow.getComputedStyle(f.contentDocument.querySelector('#stream video')).objectFit;
    await size(f, 390, 844);
    out.videoPortrait = pane(f);
    await size(f, 1280, 800);
    out.videoWide = pane(f);
    await size(f, 800, 1280);
    out.videoTall = pane(f);

    // The video, 9:16: capped at half the window in portrait, as wide as its ratio asks in landscape.
    out.errorsF = f.contentWindow.__errors.slice();
    f.remove();
    __t.frames.length = 0;
    out.post.tall = await open('http://fake.invalid/live?w=1080&h=1920');
    const g = await __t.frame('/', 390, 844, Object.assign({ __vw: '1080', __vh: '1920' }, opts));
    await settled(g, 'video');
    await sleep(500);
    out.sourceTallPortrait = pane(g);
    await size(g, 1280, 800);
    out.sourceTallWide = pane(g);
    await size(g, 390, 844);

    // Text stays text; a password stays hidden.
    const markup = 'http://fake.invalid/hang?x=<img/src=x/onerror=alert(1)>';
    out.post.markup = await open(markup);
    await until(() => pane(g).line.includes('onerror'), 8000);
    out.markup = pane(g);
    out.markup.want = markup;
    out.markup.imgs = g.contentDocument.images.length;
    out.markup.alerts = g.contentWindow.__alerts.slice();
    out.post.secret = await open('http://user:pw@fake.invalid/hang');
    await until(() => pane(g).line.includes('***@'), 8000);
    out.secret = pane(g);
    out.secret.text = g.contentDocument.getElementById('stream').textContent + '|' + (g.contentDocument.getElementById('stream').getAttribute('title') || '');

    // Closed: the pane goes, the board has the window, and no media request follows.
    await api('GET', '/__log?clear=1');
    const del = await api('DELETE', '/api/stream');
    out.deleteStatus = del.status;
    await until(() => !pane(g).streaming, 8000);
    await sleep(3000);
    out.closed = pane(g);
    const cdoc = g.contentDocument;
    out.closed.doc = { cls: cdoc.documentElement.className + '|' + cdoc.body.className, scrollW: cdoc.documentElement.scrollWidth, clientW: cdoc.documentElement.clientWidth };
    out.closed.mediaRequests = (await api('GET', '/__log')).data.filter((x) => x[1].startsWith('/api/stream/media')).length;
    out.errors = [out.errorsF, g.contentWindow.__errors.slice()];
    out.took = Date.now() - T0;
    await __t.cleanup();
    await __t.post(out);
});
