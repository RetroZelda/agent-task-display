// The needs-input reminder: while a question waits and this device has sound on, the attention chime
// repeats every remind_every seconds (3 here); it pauses while the board is out of reach and stops once
// the question is answered. The page is insecure (__insecure=1), so notifications never show, sounds do.
__t.run(async () => {
    const { sleep, $, api } = __t;
    const tid = __t.params.get('__tids').split(',')[0];
    const T0 = Date.now(), at = () => Date.now() - T0;
    const conn = [];
    let last = null;
    const iv = setInterval(() => { const s = $('conn').dataset.state; if (s !== last) { conn.push([at(), s]); last = s; } }, 100);
    await sleep(2000);
    const asked = at();
    await api('POST', '/api/tasks/' + tid + '/attention', { message: 'Shall I go on?' });
    await sleep(11000);
    const offlineAt = at();
    await fetch('/__offline?ms=9000');
    await sleep(9000);
    for (let i = 0; i < 60 && $('conn').dataset.state !== 'live'; i++) await sleep(250);
    const backAt = at();
    await sleep(4500);
    const resumed = at();
    await api('DELETE', '/api/tasks/' + tid + '/attention');
    await sleep(8000);
    clearInterval(iv);
    await __t.post({ asked, offlineAt, backAt, resumed, end: at(), conn, errors: window.__errors, notes: __rec.notes.length,
                     notifyState: $('dev-notify-state').textContent, chimes: __t.chimes().map((c) => [c.at - T0, c.kind]) });
});
