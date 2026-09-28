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
    // Runs an async test body; a crash is posted as the result so the test fails with the stack.
    run(body) {
        body().catch((e) => this.post({ crashed: String((e && e.stack) || e) }));
    },
};
