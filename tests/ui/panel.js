// Settings panel: the bell, a device cell (the needs-input pulse off: body.calm), and the All viewers tab:
// Save, the board changing it (form clean, then dirty), Save while offline, Reset, a broken file.
__t.run(async () => {
    const { sleep, $, api } = __t;
    const out = { steps: [] };
    const st = (label, extra) => out.steps.push(Object.assign({ label, notice: $('glob-notice').hidden ? null : $('glob-notice-text').textContent,
        status: $('glob-status').textContent, save: !$('glob-save').disabled, discard: !$('glob-discard').hidden, reset: !$('glob-reset').disabled,
        path: $('glob-path').textContent, exists: $('glob-exists').textContent }, extra || {}));
    await api('DELETE', '/api/settings');
    await sleep(2000);
    // bell toggles device sound
    const bell = $('bell');
    const bell0 = bell.getAttribute('aria-pressed');
    bell.click();
    const bell1 = bell.getAttribute('aria-pressed');
    out.bell = [bell0, bell1, JSON.parse(localStorage.getItem('taskstatus.settings.local')).sound_enabled, __t.chimes().map((c) => c.kind)];
    bell.click();
    // device: waiting highlight off -> calm (no pulse)
    $('settings-btn').click();
    await sleep(300);
    const hl = document.querySelector('.tri[data-key=waiting][data-channel=highlight]');
    hl.click(); hl.click();  // inherit -> on -> off
    await sleep(100);
    const waitingRow = document.querySelector('.row[data-waiting]');
    out.calm = { cls: document.body.classList.contains('calm'), anim: waitingRow && getComputedStyle(waitingRow).animationName, state: hl.dataset.state };
    hl.click();  // back to inherit
    await sleep(100);
    out.calm.after = [document.body.classList.contains('calm'), waitingRow && getComputedStyle(waitingRow).animationName];
    // global tab
    $('tab-global').click();
    await sleep(800);
    st('global loaded');
    const box = document.querySelector('#glob-matrix input[data-key=task_done][data-channel=sound]');
    box.click();
    await sleep(100);
    st('dirty');
    $('glob-save').click();
    await sleep(1200);
    const s1 = await api('GET', '/api/settings');
    st('saved', { server: s1.data.settings.events.task_done, exists: s1.data.exists });
    // changed on the board while the panel is open and clean -> "updated" notice, new values shown
    await api('PUT', '/api/settings', { settings: { events: { stale: { sound: true } } } });
    await sleep(2500);
    st('changed on the board (clean form)', { staleSound: document.querySelector('#glob-matrix input[data-key=stale][data-channel=sound]').checked });
    // dirty, then the board changes -> conflict notice
    document.querySelector('#glob-matrix input[data-key=task_started][data-channel=title]').click();
    await api('PUT', '/api/settings', { settings: { events: { request_created: { title: true } } } });
    await sleep(2500);
    st('changed on the board (dirty form)');
    // offline: Save fails, edits kept
    await fetch('/__offline?ms=4000');
    $('glob-save').click();
    await sleep(1500);
    st('save while offline', { taskStartedTitle: document.querySelector('#glob-matrix input[data-key=task_started][data-channel=title]').checked });
    await sleep(3500);
    // volume + save after the board is back
    const vol = $('glob-volume');
    vol.value = '30';
    vol.dispatchEvent(new Event('input'));
    $('glob-save').click();
    await sleep(1200);
    const s2 = await api('GET', '/api/settings');
    st('saved after reconnect', { server: { volume: s2.data.settings.sound.volume, task_started: s2.data.settings.events.task_started, request_created: s2.data.settings.events.request_created } });
    // a hand edit breaks the file (the board falls back to the defaults, a new version): the notice says so, Save is offered even with nothing changed
    await fetch('/__settings_file', { method: 'POST', body: '{"events": {"task_done": {"sound": "yes"}}}', headers: { 'X-Test': '1' } });
    for (let i = 0; i < 60 && $('glob-notice').hidden; i++) await sleep(250);  // the next poll (maybe still backing off) sees it
    st('broken file on the board', { volume: $('glob-volume').value });
    $('glob-save').click();
    await sleep(1200);
    const s4 = await api('GET', '/api/settings');
    st('saved over the broken file', { serverError: s4.data.error, exists: s4.data.exists });
    // Reset -> confirm -> DELETE
    $('glob-reset').click();
    await sleep(1200);
    const s3 = await api('GET', '/api/settings');
    st('reset', { exists: s3.data.exists, alerts: window.__alerts.slice() });
    out.errors = window.__errors;
    await __t.post(out);
});
