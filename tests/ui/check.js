// Generic page check, for the list and detail views at any width and theme:
//   1. nothing overflows the viewport or its card (no horizontal scroll)
//   2. wrapped meta lines never start with a dangling "·" separator
//   3. every number, status and title on the page matches a fresh read of the API
//   4. nothing agent-supplied became markup (no <img>, no alert, no JS error)
//   5. the running-time ticker advances
// Problems go into result.problems; test_ui.py fails on any.
__t.run(async () => {
    const { sleep, $ } = __t;
    const out = { url: location.href, problems: [] };
    const bad = (msg) => out.problems.push(msg);
    await sleep(1800);

    out.theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    out.bodyBg = getComputedStyle(document.body).backgroundColor;

    // 1. Horizontal overflow
    const de = document.documentElement, vw = de.clientWidth;
    out.viewport = vw;
    out.scrollWidth = de.scrollWidth;
    if (de.scrollWidth > vw) bad('page scrolls horizontally: scrollWidth ' + de.scrollWidth + ' > ' + vw);
    for (const el of document.querySelectorAll('body *')) {
        if (el.closest('[hidden]') || el.closest('.hint') || (el.tagName === 'IMG' && el.src.includes('__slow'))) continue;  // .hint clips with an ellipsis
        const r = el.getBoundingClientRect();
        if (!r.width) continue;
        if (r.right > vw + 0.5) bad('past the viewport: ' + el.tagName + '.' + el.className + ' [' + (el.dataset.ref || '') + '] right=' + r.right.toFixed(1) + ' "' + el.textContent.slice(0, 30) + '"');
        const box = el.parentElement && el.parentElement.closest('.row, .card');
        if (box) {
            const b = box.getBoundingClientRect(), cs = getComputedStyle(box);
            const inner = b.right - parseFloat(cs.borderRightWidth) - parseFloat(cs.paddingRight);
            if ((r.right > inner + 1 && !el.closest('.meta')) || r.right > b.right + 0.5)
                bad('past its card: ' + el.tagName + '.' + el.className + ' [' + (el.dataset.ref || '') + '] right=' + r.right.toFixed(1) + ' > ' + inner.toFixed(1));
        }
    }

    // 2. Meta lines: the first part on every line starts at the text edge (its "·" clipped away).
    let metaLines = 0, metaWrapped = 0;
    for (const meta of document.querySelectorAll('.meta')) {
        if (meta.closest('[hidden]')) continue;
        const parent = meta.parentElement, pr = parent.getBoundingClientRect(), pcs = getComputedStyle(parent);
        const textLeft = pr.left + parseFloat(pcs.borderLeftWidth) + parseFloat(pcs.paddingLeft);
        const parts = [...meta.children].filter((c) => !c.hidden && c.getBoundingClientRect().width);
        const tops = new Set(), mr = meta.getBoundingClientRect();
        const clip = getComputedStyle(meta).clipPath;
        const clipLeft = mr.left + (clip && clip !== 'none' ? parseFloat(clip.split(/\s+/).pop()) : 0);
        for (const c of parts) {
            const r = c.getBoundingClientRect(), pad = parseFloat(getComputedStyle(c).paddingLeft);
            const lineFirst = !parts.some((o) => o !== c && Math.abs(o.getBoundingClientRect().top - r.top) < 2 && o.getBoundingClientRect().left < r.left);
            if (lineFirst) {
                tops.add(Math.round(r.top));
                const before = getComputedStyle(c, '::before').content;
                if (before && before !== 'none' && before !== 'normal' && r.left >= clipLeft - 0.5) bad('dangling separator at a line start: "' + c.textContent.slice(0, 30) + '"');
                if (Math.abs(r.left + pad - textLeft) > 1) bad('meta line-first part not at the text edge: "' + c.textContent + '" ' + (r.left + pad).toFixed(1) + ' vs ' + textLeft.toFixed(1));
            }
        }
        metaLines += tops.size;
        if (tops.size > 1) metaWrapped++;
    }
    out.metaLines = metaLines;
    out.metaWrapped = metaWrapped;

    // 3. Numbers straight from the server
    const m = location.pathname.match(/^\/r\/([^/]+)/);
    const floorPct = (p) => Math.floor(p) + '%';
    const checks = { rows: 0 };
    if (!m) {
        const d = await (await fetch('/api/requests')).json();
        for (const r of d.requests) {
            const row = document.querySelector('a.row[href="/r/' + r.id + '"]');
            if (!row) { bad('missing row ' + r.id); continue; }
            checks.rows++;
            const q = (ref) => row.querySelector('[data-ref=' + ref + ']').textContent;
            const want = r.tasks_total === 0 && r.tasks_cancelled === 0 ? 'no tasks' : r.tasks_done + '/' + r.tasks_total + ' tasks';
            if (q('tasks') !== want) bad(r.id + ' tasks "' + q('tasks') + '" want "' + want + '"');
            const wantPct = r.tasks_total === 0 && r.status !== 'done' ? '—' : floorPct(r.percent);
            if (q('pct') !== wantPct) bad(r.id + ' pct "' + q('pct') + '" want "' + wantPct + '"');
            const st = r.status === 'running' && r.stale ? 'stale' : r.status;
            if (row.dataset.status !== st) bad(r.id + ' status ' + row.dataset.status + ' want ' + st);
            if (q('title') !== r.title) bad(r.id + ' title differs');
            const fw = row.querySelector('.fill').style.width;
            if (!(r.status === 'running' && r.tasks_total === 0) && fw !== r.percent + '%') bad(r.id + ' bar ' + fw + ' want ' + r.percent + '%');
            if ((r.tasks_failed ? r.tasks_failed + ' failed' : '') !== (row.querySelector('[data-ref=failed]').hidden ? '' : q('failed'))) bad(r.id + ' failed count');
            if (!row.dataset.status || !document.getElementById(r.status === 'running' ? 'list-active' : 'list-finished').contains(row)) bad(r.id + ' in the wrong group');
        }
        const c = d.counts, s = $('summary').textContent;
        const wantS = [c.running && c.running + ' running', c.stale && c.stale + ' stale', c.done && c.done + ' done', c.failed && c.failed + ' failed'].filter(Boolean).join('');
        if (s !== wantS) bad('summary "' + s + '" want "' + wantS + '"');
        out.summary = s;
        out.counts = c;
        out.finishedCount = $('count-finished').textContent;
        out.activeCount = $('count-active').textContent;
        out.title = document.title;
        out.empty = !$('list-empty').hidden;
        out.quickstart = $('quickstart').textContent;
        out.view = $('view-list').hidden ? 'not list' : 'list';
    } else {
        const rid = decodeURIComponent(m[1]).toLowerCase().replace(/-\d+$/, '');
        const res = await fetch('/api/requests/' + encodeURIComponent(rid));
        if (res.ok) {
            const d = await res.json();
            const head = $('req-head'), q = (ref) => head.querySelector('[data-ref=' + ref + ']').textContent;
            const want = d.tasks_total === 0 && d.tasks_cancelled === 0 ? 'no tasks' : d.tasks_done + '/' + d.tasks_total + ' tasks';
            if (q('tasks') !== want) bad('header tasks "' + q('tasks') + '" want "' + want + '"');
            const wantPct = d.tasks_total === 0 && d.status !== 'done' ? '—' : floorPct(d.percent);
            if (q('pct') !== wantPct) bad('header pct "' + q('pct') + '" want "' + wantPct + '"');
            if (q('title') !== d.title) bad('header title differs');
            const hst = d.status === 'running' && d.stale ? 'stale' : d.status;
            if (head.dataset.status !== hst) bad('header status ' + head.dataset.status + ' want ' + hst);
            out.header = { tasks: q('tasks'), pct: q('pct'), cancelled: q('cancelled'), time: q('time'), eta: q('eta'), origin: q('origin'),
                           message: q('message'), markDoneHidden: head.querySelector('[data-ref=markDone]').hidden };
            for (const t of d.tasks) {
                const row = document.getElementById(t.id);
                if (!row) { bad('missing task row ' + t.id); continue; }
                checks.rows++;
                const tq = (ref) => row.querySelector('[data-ref=' + ref + ']').textContent;
                const wantT = t.status === 'pending' || t.status === 'cancelled' ? '—' : floorPct(t.percent);
                if (tq('pct') !== wantT) bad(t.id + ' pct "' + tq('pct') + '" want "' + wantT + '"');
                const st = t.status === 'running' && t.stale ? 'stale' : t.status;
                if (row.dataset.status !== st) bad(t.id + ' status ' + row.dataset.status + ' want ' + st);
                if (tq('title') !== t.title) bad(t.id + ' title differs');
                if ((t.message || '') !== tq('msg')) bad(t.id + ' message differs');
                if (t.start_inferred && !/~/.test(tq('time'))) bad(t.id + ' start_inferred without ~');
            }
            out.taskEmpty = !$('task-empty').hidden;
            out.target = [...document.querySelectorAll('.row.target')].map((e) => e.id);
            out.hash = location.hash;
            out.path = location.pathname;
            out.title = document.title;
            out.view = $('view-detail').hidden ? 'not detail' : 'detail';
        } else {
            out.notFound = $('nf-title').textContent;
            out.nfText = $('nf-text').textContent;
            out.view = $('view-notfound').hidden ? 'not notfound' : 'notfound';
            out.title = document.title;
        }
    }
    out.checks = checks;

    // 4. XSS: nothing agent-supplied became markup
    out.imgs = [...document.images].filter((i) => !i.src.includes('__slow')).length;
    out.scripts = document.scripts.length;
    out.alerts = window.__alerts;
    out.errors = window.__errors;
    if (out.imgs) bad(out.imgs + ' unexpected <img> elements');
    if (window.__alerts.length) bad('alert/confirm called: ' + window.__alerts.join(' | '));
    if (window.__errors.length) bad('JS errors: ' + window.__errors.join(' | '));

    // 5. Ticker: one running "time" text sampled for 3.2s
    const tick = document.querySelector('[data-status=running] [data-ref=time], #req-head[data-status=running] [data-ref=time]');
    if (tick) {
        const seen = [tick.textContent];
        for (let i = 0; i < 16; i++) {
            await sleep(200);
            if (tick.textContent !== seen[seen.length - 1]) seen.push(tick.textContent);
        }
        out.tickSamples = seen;
    }
    out.conn = $('conn').hidden ? 'hidden' : $('conn-text').textContent;
    await __t.post(out);
});
