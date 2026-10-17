// A request's report on the dashboard, on a board of the case's own (test_ui.py seeds request A with an HTML report
// whose script probes its sandbox, B with a Markdown report, C with none). The board pages are iframes of a /__frame
// page; the report inside is another frame, of an opaque origin, which this script cannot read: the probe in A's
// report posts what it found to its parent (the board page) every 250 ms. Checked: the report view (header, links,
// the frame filling the window, its sandbox), the probe (its origin, no parent access, no storage, no reads of the
// board, a write refused, links that leave opening in a new tab, the theme), a new version reloading the frame, this
// device's theme reaching it, a Markdown report, the Report button and the list's marker, a deleted report, a
// deleted request, an unknown one.
__t.run(async () => {
    const { sleep, until, api, frame, size } = __t;
    const ids = JSON.parse(decodeURIComponent(__t.params.get('__ids')));
    const out = { ids, errors: [] };
    const drop = (x) => { out.errors.push((x.contentWindow.__errors || []).slice()); __t.drop(x); };
    const probes = new WeakMap();   // board page window -> the latest probe it was sent
    const listen = (f) => f.contentWindow.addEventListener('message', (e) => { if (e.data && e.data.probe) probes.set(f.contentWindow, e.data.probe); });
    const probe = (f) => probes.get(f.contentWindow) || null;
    const view = (f) => {
        const d = f.contentDocument, $ = (id) => d.getElementById(id), rf = $('report-frame'), w = f.contentWindow;
        return {
            shown: !$('view-report').hidden, title: $('report-title').textContent, back: [$('report-back').getAttribute('href'), $('report-back-title').textContent],
            meta: ['report-format', 'report-updated', 'report-version', 'report-size'].map((id) => ($(id).hidden ? '' : $(id).textContent)),
            artifact: $('report-artifact').hidden ? null : { href: $('report-artifact').getAttribute('href'), target: $('report-artifact').target, rel: $('report-artifact').rel },
            own: $('report-own').hidden ? null : { href: $('report-own').getAttribute('href'), target: $('report-own').target },
            none: $('report-none').hidden ? null : $('report-none').textContent,
            frame: rf.hidden ? null : { src: rf.getAttribute('src'), sandbox: rf.getAttribute('sandbox'), allow: rf.getAttribute('allow'), referrer: rf.getAttribute('referrerpolicy'),
                                        rect: __t.rect(rf) },
            vh: w.innerHeight, scroll: [d.documentElement.scrollWidth, d.documentElement.clientWidth, d.documentElement.scrollHeight],
            doc: d.title, notFound: !$('view-notfound').hidden ? $('nf-title').textContent : null,
        };
    };
    const frameSrc = (f) => f.contentDocument.getElementById('report-frame').getAttribute('src');

    // A's report, at phone size
    const f = await frame(`/r/${ids.a}/report`, 390, 844);
    listen(f);
    await until(() => probe(f) && probe(f).done, 8000);
    out.a = view(f);
    out.probe = probe(f);
    out.stillRunning = (await api('GET', `/api/requests/${ids.a}`)).data.status;
    await size(f, 1280, 800);
    await sleep(900);     // a tick or two: the frame is sized again
    out.aWide = view(f);

    // a new version: the header follows on the next poll, the frame reloads
    const doc = (await api('GET', `/api/requests/${ids.a}/report`)).data.content;
    await api('PUT', `/api/requests/${ids.a}/report`, { format: 'html', content: doc.replace('Probe report', 'Probe report v2'),
                                                        artifact_url: 'https://claude.ai/artifact/a2' });
    await until(() => (frameSrc(f) || '').includes('v=2') && probe(f) && probe(f).title === 'Probe report v2', 8000);
    out.v2 = view(f);
    out.v2probe = probe(f);

    // this device's theme reaches the report
    const d = f.contentDocument;
    d.getElementById('settings-btn').click();
    await sleep(300);
    const theme = async (value) => {
        f.contentDocument.querySelector(`input[name=dev-theme][value=${value}]`).click();
        await until(() => probe(f) && (frameSrc(f) || '').endsWith(probe(f).src), 6000);
        await sleep(300);
        return { src: frameSrc(f), probe: probe(f) };
    };
    out.dark = await theme('dark');
    out.light = await theme('light');
    out.system = await theme('system');
    d.getElementById('settings-close').click();

    // B: Markdown
    const g = await frame(`/r/${ids.b}/report`, 390, 844);
    await until(() => g.contentDocument.getElementById('report-frame').getAttribute('src'), 6000);
    await sleep(300);
    out.b = view(g);
    drop(g);

    // C: no report; the request page has no Report button, A's has one; the list marks A only
    const c = await frame(`/r/${ids.c}/report`, 390, 844);
    out.c = view(c);
    drop(c);
    const det = await frame(`/r/${ids.a}`, 390, 844);
    const btn = det.contentDocument.querySelector('#req-head [data-ref=report]');
    out.detailA = { hidden: btn.hidden, href: btn.getAttribute('href'), text: btn.textContent, title: btn.title };
    drop(det);
    const detC = await frame(`/r/${ids.c}`, 390, 844);
    out.detailC = { hidden: detC.contentDocument.querySelector('#req-head [data-ref=report]').hidden };
    drop(detC);
    const list = await frame('/', 1280, 800);
    const marker = (rid) => { const el = list.contentDocument.querySelector(`a.row[href="/r/${rid}"] [data-ref=report]`); return el && !el.hidden ? el.textContent : null; };
    out.list = { a: marker(ids.a), b: marker(ids.b), c: marker(ids.c) };
    drop(list);

    // the report deleted under the view: the frame goes, the view says how to send one
    await api('DELETE', `/api/requests/${ids.a}/report`);
    await until(() => f.contentDocument.getElementById('report-frame').hidden, 6000);
    out.deleted = view(f);
    // the request deleted: the not-found view
    await api('DELETE', `/api/requests/${ids.a}`);
    await until(() => !f.contentDocument.getElementById('view-notfound').hidden, 6000);
    out.gone = view(f);
    drop(f);
    const u = await frame('/r/zzzzzz/report', 390, 844).catch(() => null);
    await sleep(800);
    out.unknown = u ? view(u) : null;
    if (u) drop(u);
    await __t.cleanup();
    await __t.post(out);
});
