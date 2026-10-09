// Helpers for the UI test scripts; harness.py injects this before each one.
// A script does its checks in the page and posts one JSON result, which test_ui.py asserts on.
window.__t = {
    params: new URLSearchParams(window.__testQuery || location.search),  // the page may rewrite its URL
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    $: (id) => document.getElementById(id),
    post(result) {
        const name = this.params.get('__name') || 'result';
        return fetch('/__result?name=' + encodeURIComponent(name), { method: 'POST', body: JSON.stringify(result) });
    },
    // An API call from the test itself: it bypasses the harness's offline mode (X-Test).
    api(method, path, body, headers) {
        const opts = { method, headers: Object.assign({ 'Content-Type': 'application/json', 'X-Test': '1' }, headers || {}) };
        if (body !== undefined) opts.body = JSON.stringify(body);
        return fetch(path, opts).then(async (r) => ({ status: r.status, data: await r.json().catch(() => null) }));
    },
    // Pretends the tab went to the background (true) or came back (false); doc: another document (a frame's).
    hide(value, doc = document) {
        Object.defineProperty(doc, 'hidden', { configurable: true, get: () => value });
        Object.defineProperty(doc, 'visibilityState', { configurable: true, get: () => (value ? 'hidden' : 'visible') });
        doc.dispatchEvent(new doc.defaultView.Event('visibilitychange'));
    },
    // Polls fn until it is truthy (and returns that), or ms pass (null).
    async until(fn, ms = 5000, step = 100) {
        const end = Date.now() + ms;
        for (;;) {
            let value = null;
            try {
                value = fn();
            } catch (e) { /* not there yet */ }
            if (value) return value;
            if (Date.now() >= end) return null;
            await this.sleep(step);
        }
    },
    // An element's box as plain numbers.
    rect(el) {
        const r = el.getBoundingClientRect();
        return { x: r.left, y: r.top, w: r.width, h: r.height };
    },
    // The stream cases load boards in iframes of a /__frame page: each loads and attaches as for a user, while this
    // page's slow image alone holds the screenshot. The board has the shim (and opts: more shim options, as an object,
    // __media: 'stub' among them) but no test script. Resolves once it has had its first poll answered.
    frames: [],
    async frame(path, w, h, opts) {
        const f = document.createElement('iframe');
        const used = this.frames.reduce((x, o) => Math.max(x, o.offsetLeft + o.offsetWidth), 0);
        f.style.cssText = 'top:0;left:' + (this.frames.length ? used + 4 : 0) + 'px;width:' + w + 'px;height:' + h + 'px';
        const [page, hash] = path.split('#');
        f.src = page + (page.includes('?') ? '&' : '?') + new URLSearchParams(Object.assign({ __shim: '1' }, opts || {})) + (hash ? '#' + hash : '');
        document.body.appendChild(f);
        this.frames.push(f);
        await new Promise((resolve) => f.addEventListener('load', resolve, { once: true }));
        await this.until(() => f.contentDocument.getElementById('conn').dataset.state === 'live', 8000);
        return f;
    },
    // Takes one frame away (frames of one page share its localStorage: a board loaded later would change the first one's settings).
    drop(f) {
        f.remove();
        const at = this.frames.indexOf(f);
        if (at >= 0) this.frames.splice(at, 1);
    },
    // Resizes a frame: the board's media and container queries follow its new viewport.
    async size(f, w, h) {
        f.style.width = w + 'px';
        f.style.height = h + 'px';
        await this.sleep(250);
    },
    // What a frame's stream pane shows now.
    pane(f) {
        const d = f.contentDocument, w = f.contentWindow, el = d.getElementById('stream'), video = el.querySelector('video');
        const chip = d.getElementById('stream-sound'), board = d.getElementById('board');
        return {
            streaming: d.body.classList.contains('streaming'), hidden: el.hidden, state: el.dataset.state, held: el.dataset.held || null,
            line: d.getElementById('stream-line').textContent, timer: d.getElementById('stream-timer').textContent,
            title: el.getAttribute('title'), rect: this.rect(el), board: this.rect(board), vw: w.innerWidth, vh: w.innerHeight,
            bg: w.getComputedStyle(el).backgroundColor, ar: el.style.getPropertyValue('--ar'),
            chip: chip.hidden ? null : { text: d.getElementById('stream-chip-text').textContent, label: chip.getAttribute('aria-label'),
                                         rect: this.rect(chip), textShown: w.getComputedStyle(d.getElementById('stream-chip-text')).display !== 'none' },
            videos: el.querySelectorAll('video').length,
            video: video && { muted: video.muted, paused: video.paused, volume: video.volume, rect: this.rect(video),
                              opacity: w.getComputedStyle(video).opacity, w: video.videoWidth, h: video.videoHeight },
            errors: (w.__errors || []).slice(),      // a page reached by a link has no shim
        };
    },
    // Takes the frames away and closes the stream (the board's and the injected one), so no media request is left in
    // flight when the script posts its result. With __shot=1 (a human look at the screenshot) it leaves them as they are.
    async cleanup() {
        if (this.params.get('__shot')) return;
        for (const f of this.frames.splice(0)) f.remove();
        await this.api('DELETE', '/api/stream').catch(() => null);
        await this.api('GET', '/__stream?off=1').catch(() => null);
        await this.sleep(300);
    },
    // The chimes the stubbed AudioContext played (harness __stub=1): the tones one chime starts come within
    // a few milliseconds of each other (a busy machine can put a millisecond between them; chimes are 2s
    // apart), and its first frequency names it.
    chimes() {
        const kinds = { 784: 'attention', 440: 'failure', 659.3: 'success', 1046.5: 'info' };
        const out = [];
        for (const t of window.__rec.tones) {
            const last = out[out.length - 1];
            if (last && t.at - last.at <= 50) continue;
            out.push({ kind: kinds[t.f] || String(t.f), at: t.at });
        }
        return out;
    },
    // Runs an async test body; a crash is posted as the result so the test fails with the stack.
    run(body) {
        body().catch((e) => this.cleanup().then(() => this.post({ crashed: String((e && e.stack) || e) })));
    },
};
// The helpers call each other through `this`: bound, so a script may take them apart (const { until, pane } = __t).
for (const name of Object.keys(window.__t)) if (typeof window.__t[name] === 'function') window.__t[name] = window.__t[name].bind(window.__t);
