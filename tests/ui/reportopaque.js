// A report on a board without a report port (test_ui.py): the frame's page is the board's own, in an opaque origin. The
// report's probe posts what it found to its parent, this page.
__t.run(async () => {
    const { until, $ } = __t;
    let probe = null;
    addEventListener('message', (e) => { if (e.data && e.data.probe) probe = e.data.probe; });
    await until(() => probe && probe.done, 8000);
    const f = $('report-frame');
    await __t.post({ src: f.src, sandbox: f.getAttribute('sandbox'), probe, errors: window.__errors });
});
