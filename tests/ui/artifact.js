// An artifact page as claude.ai serves it (/__fixture/NAME: its skeleton and all, unsandboxed) beside the board's page of
// the same artifact sent as a report (its page on the board's report port, __rport, framed as the dashboard frames it), both W
// pixels wide and as tall as the page, so test_ui.py can compare the screenshot's two halves pixel for pixel. A page
// taller than one screenshot is compared a slice at a time: __offset and __slice show rows offset to offset + slice of
// both, left at x 0 and right at the next multiple of 256 after W + 10. The reference's fonts are awaited, then a moment more for the board's
// copy, which takes the same fonts from the cache.
__t.run(async () => {
    const { sleep, until } = __t;
    const p = (k) => __t.params.get(k);
    // the right side starts on a multiple of 256px: Firefox rasterizes in tiles of that size, and a long SVG line drawn
    // across another tile alignment comes out with a few pixels anti-aliased differently, though nothing else differs
    const W = Number(p('__w')), OFFSET = Number(p('__offset') || 0), SLICE = Number(p('__slice')), RIGHT = Math.ceil((W + 10) / 256) * 256;
    const SANDBOX = 'allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms allow-pointer-lock';
    document.documentElement.style.cssText = 'margin:0;padding:0;background:#808080';
    document.body.style.cssText = 'margin:0;padding:0';
    const clip = (x) => {
        const box = document.createElement('div');
        box.style.cssText = `position:absolute;top:0;left:${x}px;width:${W}px;height:${SLICE}px;overflow:hidden`;
        document.body.appendChild(box);
        return box;
    };
    const frame = (box, src, sandbox) => new Promise((resolve) => {
        const f = document.createElement('iframe');
        f.style.cssText = `position:absolute;left:0;top:${-OFFSET}px;width:${W}px;height:${SLICE}px;border:0;display:block`;
        if (sandbox) {
            f.setAttribute('sandbox', sandbox);
            f.setAttribute('allow', 'fullscreen; clipboard-write');
        }
        f.addEventListener('load', () => resolve(f), { once: true });
        f.src = src;
        box.appendChild(f);
    });
    const out = { steps: [] };
    const t0 = Date.now(), step = (label) => out.steps.push([label, Date.now() - t0]);
    const within = (promise, ms) => Promise.race([promise, sleep(ms)]);
    const a = await frame(clip(0), '/__fixture/' + encodeURIComponent(p('__ref')), null);
    step('reference loaded');
    const doc = a.contentDocument;
    // an explicit theme: claude.ai's viewer sets data-theme on the page's root, as the board's page does from ?theme=
    if (p('__forced')) doc.documentElement.setAttribute('data-theme', p('__forced'));
    // fonts.ready may never settle for a frame's document here: the fonts' status is polled instead, for at most 6s
    await until(() => doc.fonts.status === 'loaded', 6000, 100);
    step('fonts');
    await within(Promise.all([...doc.images].map((img) => img.decode().catch(() => null))), 2000);
    step('images');
    await sleep(300);
    // the page's own height: measured in a short frame (scrollHeight is never less than the viewport), then grown to it
    a.style.height = '100px';
    await sleep(100);
    out.height = doc.documentElement.scrollHeight;
    a.style.height = out.height + 'px';
    await sleep(200);
    out.height = Math.max(out.height, doc.documentElement.scrollHeight);
    out.fontStatus = doc.fonts.status;
    out.scroll = [doc.documentElement.scrollWidth, doc.documentElement.clientWidth];
    out.fonts = [...doc.fonts].filter((f) => f.status === 'loaded').map((f) => f.family).sort();
    out.images = [...doc.images].map((img) => [img.naturalWidth, img.naturalHeight]);
    a.style.height = out.height + 'px';
    const theme = p('__forced') ? '?theme=' + p('__forced') : '';
    const page = `http://${location.hostname}:${p('__rport')}/api/requests/${p('__rid')}/report/page${theme}`;
    // __self=1: the reference on both sides, a control for the comparison itself
    const b = await within(p('__self') ? frame(clip(RIGHT), '/__fixture/' + encodeURIComponent(p('__ref')), null)
                                       : frame(clip(RIGHT), page, SANDBOX + ' allow-same-origin'), 4000);
    step('board page loaded');
    if (b) b.style.height = out.height + 'px';
    else out.boardFrame = 'no load event';
    await sleep(Number(p('__bwait') || 3000));     // the board's copy loads its fonts too
    out.errors = window.__errors;
    await __t.post(out);
});
