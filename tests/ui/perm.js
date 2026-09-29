// Notification permission flow: "default" -> Enable -> granted, notify_enabled, a test notification; Turn off/on.
__t.run(async () => {
    const { sleep, $ } = __t;
    await sleep(1500);
    $('settings-btn').click();
    await sleep(300);
    const out = { before: [$('dev-notify-state').textContent, $('dev-notify').textContent, $('dev-notify').disabled, $('dev-notify-test').hidden] };
    $('dev-notify').click();
    await sleep(300);
    out.after = [$('dev-notify-state').textContent, $('dev-notify').textContent, $('dev-notify-test').hidden, __rec.perms, __rec.notes.map((n) => n.title),
                 JSON.parse(localStorage.getItem('taskstatus.settings.local')).notify_enabled];
    $('dev-notify').click();
    await sleep(200);
    out.off = [$('dev-notify-state').textContent, $('dev-notify').textContent, JSON.parse(localStorage.getItem('taskstatus.settings.local')).notify_enabled];
    // keyboard: Esc closes (native dialog), arrow keys switch tabs
    $('tab-device').focus();
    $('tab-device').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    await sleep(100);
    out.arrow = [$('tab-global').getAttribute('aria-selected'), $('pane-global').hidden, document.activeElement && document.activeElement.id];
    $('settings-close').click();
    out.closed = !$('settings').open;
    out.errors = window.__errors;
    await __t.post(out);
});
