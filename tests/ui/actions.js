// Detail view: "Mark done" (confirm answers yes) closes the request on the server and the page follows.
__t.run(async () => {
    const { sleep, $ } = __t;
    await sleep(1500);
    const rid = location.pathname.split('/')[2];
    const head = $('req-head'), btn = head.querySelector('[data-ref=markDone]');
    const hiddenBefore = btn.hidden;
    btn.click();
    await sleep(2500);
    const d = await (await fetch('/api/requests/' + rid)).json();
    await __t.post({
        hiddenBefore, status: d.status, message: d.message, tasks: d.tasks.map((t) => t.id + ':' + t.status),
        badge: head.querySelector('[data-ref=badge]').textContent, headStatus: head.dataset.status,
        markDoneHidden: btn.hidden, markFailedHidden: head.querySelector('[data-ref=markFailed]').hidden,
        taskBadges: [...document.querySelectorAll('#task-list .badge')].map((b) => b.textContent),
        header: head.querySelector('[data-ref=tasks]').textContent + ' | ' + head.querySelector('[data-ref=cancelled]').textContent,
        message_shown: head.querySelector('[data-ref=message]').textContent,
        alerts: window.__alerts, errors: window.__errors,
    });
});
