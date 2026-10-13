// This device's theme (Settings > This device > Appearance): System follows the browser's own light or dark setting, Light and
// Dark stay whatever it says. The harness presets the stored choice (__local) and Firefox's own theme is set by the case; the
// script records how the page looks at load with the panel open (the attribute the colours key on, the colours the page really
// has, the root's color-scheme, the checked radio), then each radio in turn (and what is stored), then another document of
// this browser changing the stored choice (the storage event: an unknown value counts as System), and ends where it began, so
// a screenshot shows the preset's look with the panel open.
__t.run(async () => {
    const { sleep, $ } = __t;
    const KEY = 'taskstatus.settings.local';
    const root = document.documentElement;
    const read = (storage) => { try { return JSON.parse(storage.getItem(KEY) || 'null'); } catch (e) { return 'bad'; } };
    const look = () => {
        const body = getComputedStyle(document.body);
        const checked = document.querySelector('input[name=dev-theme]:checked');
        return { attr: root.dataset.theme === undefined ? null : root.dataset.theme, systemDark: matchMedia('(prefers-color-scheme: dark)').matches,
                 bodyBg: body.backgroundColor, bodyColor: body.color, scheme: getComputedStyle(root).colorScheme,
                 dialogBg: getComputedStyle($('settings')).backgroundColor, radio: checked ? checked.value : null,
                 radios: [...document.querySelectorAll('input[name=dev-theme]')].map((r) => r.value), note: $('dev-theme-note').textContent,
                 stored: (read(localStorage) || {}).theme === undefined ? null : read(localStorage).theme };
    };
    const out = {};
    // the head script comes before the stylesheet, so the stored choice is in place before the first paint
    const headScript = [...document.head.querySelectorAll('script')].find((s) => s.textContent.includes(KEY));
    out.firstSet = window.__themeFirst || null;  // the shim saw <html> get its data-theme: its value, and whether <body> existed yet
    out.headScriptFirst = !!headScript && !!(headScript.compareDocumentPosition(document.head.querySelector('style')) & Node.DOCUMENT_POSITION_FOLLOWING);
    $('settings-btn').click();
    await sleep(500);
    out.initial = look();
    const pick = async (value) => {
        document.querySelector(`input[name=dev-theme][value=${value}]`).click();
        await sleep(200);
        return look();
    };
    out.dark = await pick('dark');
    out.light = await pick('light');
    out.system = await pick('system');
    // another document of this browser (a tab, here a same-origin frame) changes the stored choice
    const frame = document.createElement('iframe');
    frame.style.display = 'none';
    document.body.appendChild(frame);
    const other = frame.contentWindow.localStorage;
    const write = async (theme) => {
        const stored = read(other) || {};
        stored.theme = theme;
        other.setItem(KEY, JSON.stringify(stored));
        await sleep(500);
        return look();
    };
    out.otherDark = await write('dark');
    out.otherLight = await write('light');
    out.otherSystem = await write('system');
    out.otherJunk = await write('dark').then(() => write('sepia'));
    frame.remove();
    out.back = await pick(out.initial.radio);
    out.errors = window.__errors;
    await __t.post(out);
});
