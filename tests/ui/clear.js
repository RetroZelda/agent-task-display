// List view: "Clear" (confirm answers yes) deletes every finished request listed; the running ones
// stay, and clicking the button inside <summary> must not toggle the Finished group.
__t.run(async () => {
    const { sleep, $ } = __t;
    await sleep(1200);
    const finishedBefore = document.querySelectorAll('#list-finished > .row').length;
    const activeBefore = document.querySelectorAll('#list-active > .row').length;
    const openBefore = $('group-finished').open;
    $('clear').click();
    await sleep(3500);
    const d = await (await fetch('/api/requests')).json();
    await __t.post({
        finishedBefore, activeBefore, openBefore, openAfter: $('group-finished').open,
        finishedAfter: document.querySelectorAll('#list-finished > .row').length,
        activeAfter: document.querySelectorAll('#list-active > .row').length,
        groupHidden: $('group-finished').hidden, apiFinished: d.counts.done + d.counts.failed, apiRunning: d.counts.running,
        alerts: window.__alerts, errors: window.__errors,
    });
});
