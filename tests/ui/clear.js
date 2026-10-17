// List view: "Clear" (confirm answers yes) deletes every finished request, on every page (the board has more than
// one page of them), not only the page shown; the running ones stay, and clicking the button inside <summary> must
// not toggle the Finished group.
__t.run(async () => {
    const { sleep, $ } = __t;
    await sleep(1200);
    const before = await (await fetch('/api/requests')).json();
    const finishedBefore = before.counts.done + before.counts.failed;
    const rowsBefore = document.querySelectorAll('#list-finished > .row').length;
    const countBefore = $('count-finished').textContent;
    const activeBefore = document.querySelectorAll('#list-active > .row').length;
    const openBefore = $('group-finished').open;
    $('clear').click();
    await sleep(3500);
    const d = await (await fetch('/api/requests')).json();
    await __t.post({
        finishedBefore, rowsBefore, countBefore, openBefore, openAfter: $('group-finished').open, activeBefore,
        finishedAfter: document.querySelectorAll('#list-finished > .row').length,
        activeAfter: document.querySelectorAll('#list-active > .row').length,
        groupHidden: $('group-finished').hidden, apiFinished: d.counts.done + d.counts.failed, apiRunning: d.counts.running,
        alerts: window.__alerts, errors: window.__errors,
    });
});
