// Reduced motion (the browser preference): what waits gets a static strong outline instead of the pulse.
__t.run(async () => {
    const { sleep, $ } = __t;
    await sleep(2000);
    const rows = [...document.querySelectorAll('.row[data-waiting], .card[data-waiting=request]')];
    await __t.post({
        reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
        rows: rows.map((r) => ({ id: r.id || r.getAttribute('href'), animation: getComputedStyle(r).animationName, shadow: getComputedStyle(r).boxShadow })),
        errors: window.__errors,
    });
});
