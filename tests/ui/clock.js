// The header clock (Settings > This device > Appearance > Clock). The board runs in one time zone and the browser in another
// (the case sets both), so a clock in the wrong zone shows. The script reads the clock and the header's boxes, lets it tick,
// then picks each radio in turn, then has another document of this browser write the choice (the storage event), and ends
// where it began, so a screenshot shows the preset's header with the panel open.
__t.run(async () => {
    const { sleep, $, api } = __t;
    const KEY = 'taskstatus.settings.local';
    const box = (el) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom };
    };
    const textOf = (el) => { const range = document.createRange(); range.selectNodeContents(el); return range; };  // the text's own box
    const look = () => {
        const el = $('clock'), checked = document.querySelector('input[name=dev-clock]:checked');
        let stored = null;
        try { stored = (JSON.parse(localStorage.getItem(KEY) || 'null') || {}).clock || null; } catch (e) { stored = 'bad'; }
        return { hidden: el.hidden, source: el.dataset.source || null, offset: el.dataset.offset === undefined ? null : Number(el.dataset.offset),
                 epoch: el.dataset.epoch === undefined ? null : Number(el.dataset.epoch), day: $('clock-day').textContent, time: $('clock-time').textContent,
                 zone: $('clock-zone').textContent, title: el.title, role: el.getAttribute('role'), clientNow: Date.now() / 1000,
                 radio: checked ? checked.value : null, note: $('dev-clock-note').textContent, stored,
                 release: { hidden: $('release').hidden, text: $('release').textContent, title: $('release').title },
                 boxes: { clock: el.hidden ? null : box(el), top: box(document.querySelector('.top')), brand: box(document.querySelector('.brand')), conn: box($('conn')),
                          connText: box($('conn-text')), release: $('release').hidden ? null : box($('release')),
                          releaseText: $('release').hidden ? null : box(textOf($('release'))),
                          bell: box($('bell')), gear: box($('settings-btn')), setup: box(document.querySelector('.setup')) },
                 scroll: [document.documentElement.scrollWidth, document.documentElement.clientWidth] };
    };
    const out = {};
    await sleep(1200);
    const health = (await api('GET', '/api/health')).data;
    out.start = { apiNow: health.now, clientNow: Date.now() / 1000, release: health.release };
    $('settings-btn').click();
    await sleep(500);
    out.initial = look();
    await sleep(1300);
    out.later = look();     // it ticks: the same clock a second or more on
    const pick = async (value) => {
        document.querySelector(`input[name=dev-clock][value=${value}]`).click();
        await sleep(300);
        return look();
    };
    out.toDevice = await pick('device');
    out.toBoard = await pick('board');
    // another document of this browser (a tab, here a same-origin frame) writes the choice
    const frame = document.createElement('iframe');
    frame.style.display = 'none';
    document.body.appendChild(frame);
    const other = frame.contentWindow.localStorage;
    const write = async (clock) => {
        const stored = JSON.parse(other.getItem(KEY) || 'null') || {};
        stored.clock = clock;
        other.setItem(KEY, JSON.stringify(stored));
        await sleep(700);
        return look();
    };
    out.otherDevice = await write('device');
    out.otherBoard = await write('board');
    out.otherJunk = await write('device').then(() => write('sundial'));
    frame.remove();
    out.back = await pick(out.initial.radio);
    out.errors = window.__errors;
    await __t.post(out);
});
