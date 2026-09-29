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
    // Pretends the tab went to the background (true) or came back (false).
    hide(value) {
        Object.defineProperty(document, 'hidden', { configurable: true, get: () => value });
        Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (value ? 'hidden' : 'visible') });
        document.dispatchEvent(new Event('visibilitychange'));
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
        body().catch((e) => this.post({ crashed: String((e && e.stack) || e) }));
    },
};
