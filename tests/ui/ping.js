// Preflight: Firefox loads a page from the harness and runs an injected script.
__t.run(async () => {
    await __t.post({ ok: true, userAgent: navigator.userAgent });
});
