// Every poll fails (network error) after the first success: "reconnecting" after one failure, the
// offline banner after two, with exponential backoff between the attempts.
__t.run(async () => {
    const { sleep, $ } = __t;
    await sleep(1000);
    const times = [];
    XMLHttpRequest.prototype.send = function () {
        times.push(Date.now());
        setTimeout(() => this.onerror && this.onerror(), 20);
    };
    const states = [];
    let bannerText = null;
    for (let i = 0; i < 40; i++) {
        await sleep(250);
        const s = $('conn').dataset.state;
        if (states[states.length - 1] !== s) states.push(s);
        if (!$('banner').hidden) bannerText = $('banner-text').textContent;
    }
    await __t.post({
        states, gaps: times.slice(1).map((t, i) => t - times[i]), attempts: times.length, bannerText,
        bannerHidden: $('banner').hidden, offlineClass: document.body.classList.contains('offline'),
        connText: $('conn-text').textContent, errors: window.__errors,
    });
});
