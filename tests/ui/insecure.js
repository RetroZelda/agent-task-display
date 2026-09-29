// Settings panel on a page that is not a secure context (__insecure=1): the guidance, and the link to
// the board's https listener when it has one (the same path on https://<host>:<tls_port>).
__t.run(async () => {
    const { sleep, $ } = __t;
    await sleep(1800);
    $('settings-btn').click();
    await sleep(600);
    await __t.post({
        open: $('settings').open, insecure: !$('insecure').hidden, tlsShown: !$('insecure-tls').hidden,
        link: $('tls-link').getAttribute('href'), linkText: $('tls-link').textContent, notifyState: $('dev-notify-state').textContent,
        notifyDisabled: $('dev-notify').disabled, errors: window.__errors,
    });
});
