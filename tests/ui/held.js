// The browser holds the audio (a suspended AudioContext: no click on the page yet, __suspended=1) while a
// question waits and its reminders fall due (remind_every 2s): nothing is scheduled meanwhile, and the
// first click plays one chime, the most urgent, instead of every held one at once; then the reminders
// go on at their pace.
__t.run(async () => {
    const { sleep, $, api } = __t;
    const tid = __t.params.get('__tids').split(',')[0];
    await sleep(1500);
    const bellBefore = $('bell').getAttribute('data-held');
    await api('POST', '/api/tasks/' + tid + '/attention', { message: 'Shall I go on?' });
    await api('POST', '/api/tasks/' + __t.params.get('__tids').split(',')[1] + '/complete', { status: 'failed', message: 'x' });
    await sleep(8000);
    const whileHeld = { tones: __rec.tones.length, nodes: __rec.nodes };
    __rec.gesture = true;
    const clickAt = Date.now();
    document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
    await sleep(700);
    const onClick = __t.chimes().map((c) => [c.at - clickAt, c.kind]);
    await sleep(6500);
    await __t.post({
        bellBefore, whileHeld, onClick, all: __t.chimes().map((c) => [c.at - clickAt, c.kind]),
        bellAfter: $('bell').getAttribute('data-held'), errors: window.__errors,
    });
});
