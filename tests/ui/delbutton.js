// Detail view: "Delete" (confirm answers yes) deletes the request and goes back to the list. The page
// navigates away, so test_ui.py checks the outcome in the harness's request log and the API.
__t.run(async () => {
    const { sleep, $ } = __t;
    await sleep(1200);
    await __t.post({ clicking: true, title: $('req-head').querySelector('[data-ref=title]').textContent });
    $('req-head').querySelector('[data-ref=del]').click();
});
