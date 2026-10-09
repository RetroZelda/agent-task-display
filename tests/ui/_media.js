// A fake MediaSource, SourceBuffer and media element, for the stream cases (harness option __media=stub): the page's
// player runs against it without a decoder, and every call it makes is recorded in window.__rec.media.
//
// It is faithful where the player depends on it. It reads the MP4 boxes appended to it (the track ids and timescales
// of the moov; each moof's track fragments with their times, durations and whether the first sample is a keyframe,
// by the flag rules of the standard) and buffers by their times; the buffered ranges are the intersection of the
// tracks', so a track that stops freezes them. remove() runs on to the next keyframe of each track, and to the end of
// the data where there is none, as the standard says. The element plays in real time (__rate=N: N times faster),
// waits where there is no data, and a media time that no append covers stays unplayable: the first seek must land
// in the buffered range. Unmuting without a gesture pauses it, as Chrome and Firefox do; __rec.gesture = true is
// "the user has interacted" (navigator.userActivation.isActive and hasBeenActive follow it).
//
// Options (query): __mse=ms|mms|both|none  which of MediaSource and ManagedMediaSource exist (ms)
//                  __autoplay=allowed|muted|blocked  what play() may do before a gesture: anything, only muted, nothing
//                  __vw, __vh  the video's size (1920x1080)      __rate=N  media seconds per real second (1)
//                  __open=never  sourceopen never fires
(function () {
    var q = new URLSearchParams(location.search);
    var mseMode = q.get('__mse') || 'ms', autoplay = q.get('__autoplay') || 'allowed';
    var VW = Number(q.get('__vw')) || 1920, VH = Number(q.get('__vh')) || 1080, RATE = Number(q.get('__rate')) || 1;
    var rec = window.__rec.media = {
        elements: 0, created: 0, urls: [], revoked: [], srcs: [], loads: 0, supports: [], opens: 0, inits: 0, plays: [], pauses: [],
        unmutes: [], seeks: [], appends: [], removes: [], aborts: 0, fragments: [], events: [], vw: VW, vh: VH,
        failNext: '',       // 'quota' makes the next append throw QuotaExceededError
        hold: false,        // true: the playhead stands still while the element "plays" (a decoder that cannot keep up)
        refuseUnmute: false, // true: the browser pauses the element after any unmute, gesture or not
        fetches: [],        // [time, path, status] of the page's requests for the media
        fail: function (message) { fire(first(), 'error', { message: message || 'decode error' }); },
        resizeTo: function (w, h) { rec.vw = w; rec.vh = h; live.forEach(function (el) { queue(el, 'resize'); }); },
        endStreaming: function () { live.forEach(function (el) { var ms = st(el).ms; if (ms && ms.streaming !== undefined) { ms.streaming = false; ms.dispatchEvent(new Event('endstreaming')); } }); },
        startStreaming: function () { live.forEach(function (el) { var ms = st(el).ms; if (ms && ms.streaming !== undefined) { ms.streaming = true; ms.dispatchEvent(new Event('startstreaming')); } }); },
        // The first element's state, for a script to read at a glance.
        snap: function () {
            var el = first(), s = el && st(el);
            return s && { ct: s.ct, paused: s.paused, muted: s.muted, volume: el.volume, ready: s.ready, seeking: s.seeking, buffered: ranges(s), meta: s.meta, vw: videoWidth(s), vh: videoHeight(s) };
        },
    };
    var live = new Set();       // the elements attached to a fake MediaSource
    var states = new WeakMap();
    function first() { return live.values().next().value || null; }
    function st(el) {
        var s = states.get(el);
        if (!s) {
            s = { ms: null, sb: null, paused: true, muted: false, ct: 0, seeking: false, ready: 0, meta: false, last: performance.now(), tu: 0 };
            states.set(el, s);
            rec.elements++;
        }
        return s;
    }
    function queue(el, type) { setTimeout(function () { fire(el, type); }, 0); }
    function fire(el, type, error) {
        if (!el) return;
        rec.events.push([Date.now(), type]);
        if (error) st(el).error = error;
        el.dispatchEvent(new Event(type));
    }
    function dom(name, type) { try { return new DOMException(name, type); } catch (e) { var x = new Error(name); x.name = type; return x; } }

    // ---- MP4 boxes
    function children(u8, start, end) {
        var out = [], pos = start, dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
        while (pos + 8 <= end) {
            var size = dv.getUint32(pos), hdr = 8;
            var type = String.fromCharCode(u8[pos + 4], u8[pos + 5], u8[pos + 6], u8[pos + 7]);
            if (size === 1) { size = Number(dv.getBigUint64(pos + 8)); hdr = 16; } else if (size === 0) size = end - pos;
            if (size < hdr || pos + size > end) break;
            out.push({ type: type, start: pos + hdr, end: pos + size });
            pos += size;
        }
        return out;
    }
    function find(u8, box, type) { return children(u8, box.start, box.end).filter(function (b) { return b.type === type; }); }
    // A new init segment keeps what is buffered, as a browser does for the same tracks.
    function readInit(u8, moov, sb) {
        var dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength), old = sb.tracks;
        sb.tracks = {};
        find(u8, moov, 'trak').forEach(function (trak) {
            var tkhd = find(u8, trak, 'tkhd')[0], mdia = find(u8, trak, 'mdia')[0];
            if (!tkhd || !mdia) return;
            var id = dv.getUint32(tkhd.start + (u8[tkhd.start] === 1 ? 20 : 12));
            var hdlr = find(u8, mdia, 'hdlr')[0], mdhd = find(u8, mdia, 'mdhd')[0];
            var kind = String.fromCharCode(u8[hdlr.start + 8], u8[hdlr.start + 9], u8[hdlr.start + 10], u8[hdlr.start + 11]);
            var scale = dv.getUint32(mdhd.start + (u8[mdhd.start] === 1 ? 20 : 12));
            sb.tracks[id] = { id: id, kind: kind, scale: scale || 1, frags: old[id] ? old[id].frags : [], dur: 0, size: 0, flags: 0 };
        });
        find(u8, moov, 'mvex').forEach(function (mvex) {
            find(u8, mvex, 'trex').forEach(function (trex) {
                var t = sb.tracks[dv.getUint32(trex.start + 4)];
                if (t) { t.dur = dv.getUint32(trex.start + 12); t.size = dv.getUint32(trex.start + 16); t.flags = dv.getUint32(trex.start + 20); }
            });
        });
    }
    // One moof: [{id, t, d, key}] per track fragment; the keyframe rule is the standard's (and the server's).
    function readFragment(u8, moof, sb) {
        var dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength), out = [];
        find(u8, moof, 'traf').forEach(function (traf) {
            var tfhd = find(u8, traf, 'tfhd')[0], tfdt = find(u8, traf, 'tfdt')[0], run = find(u8, traf, 'trun')[0];
            if (!tfhd || !run) return;
            var flags = dv.getUint32(tfhd.start) & 0xffffff, id = dv.getUint32(tfhd.start + 4), at = tfhd.start + 8;
            var track = sb.tracks[id];
            if (!track) return;
            var dur = track.dur, dflags = track.flags;
            if (flags & 0x1) at += 8;
            if (flags & 0x2) at += 4;
            if (flags & 0x8) { dur = dv.getUint32(at); at += 4; }
            if (flags & 0x10) at += 4;
            if (flags & 0x20) { dflags = dv.getUint32(at); at += 4; }
            var base = 0;
            if (tfdt) base = u8[tfdt.start] === 1 ? Number(dv.getBigUint64(tfdt.start + 4)) : dv.getUint32(tfdt.start + 4);
            var rf = dv.getUint32(run.start) & 0xffffff, count = dv.getUint32(run.start + 4), p = run.start + 8, lead = null, total = 0;
            if (rf & 0x1) p += 4;
            if (rf & 0x4) { lead = dv.getUint32(p); p += 4; }       // first_sample_flags
            for (var i = 0; i < count; i++) {
                var d = dur, f = dflags;
                if (rf & 0x100) { d = dv.getUint32(p); p += 4; }
                if (rf & 0x200) p += 4;
                if (rf & 0x400) { f = dv.getUint32(p); p += 4; if (i === 0 && lead === null) lead = f; }
                if (rf & 0x800) p += 4;
                total += d;
            }
            if (lead === null) lead = dflags;
            out.push({ id: id, t: base / track.scale, d: total / track.scale, key: track.kind !== 'vide' || !(lead & 0x10000) });
        });
        return out;
    }

    // ---- time ranges
    function merge(frags) {
        var out = [];
        frags.slice().sort(function (a, b) { return a.t - b.t; }).forEach(function (f) {
            var last = out[out.length - 1];
            if (last && f.t - last.end < 0.03) last.end = Math.max(last.end, f.t + f.d);
            else out.push({ start: f.t, end: f.t + f.d });
        });
        return out;
    }
    function intersect(a, b) {
        var out = [];
        a.forEach(function (x) { b.forEach(function (y) { var s = Math.max(x.start, y.start), e = Math.min(x.end, y.end); if (e > s) out.push({ start: s, end: e }); }); });
        return out;
    }
    // The buffered ranges of a SourceBuffer: what every track has.
    function sbRanges(sb) {
        var ids = Object.keys(sb.tracks), out = null;
        ids.forEach(function (id) { var r = merge(sb.tracks[id].frags); out = out === null ? r : intersect(out, r); });
        return out || [];
    }
    function ranges(s) { return s.sb ? sbRanges(s.sb).map(function (r) { return [r.start, r.end]; }) : []; }
    function timeRanges(list) {
        return { length: list.length, start: function (i) { return list[i].start; }, end: function (i) { return list[i].end; } };
    }
    function videoWidth(s) { return s.sb && s.meta && Object.keys(s.sb.tracks).some(function (id) { return s.sb.tracks[id].kind === 'vide'; }) ? rec.vw : 0; }
    function videoHeight(s) { return videoWidth(s) ? rec.vh : 0; }

    // ---- MediaSource
    class FakeMS extends EventTarget {
        constructor() {
            super();
            this.readyState = 'closed';
            this.sourceBuffers = [];
            this.duration = NaN;
            rec.created++;
        }
        static isTypeSupported(type) {
            rec.supports.push(String(type));
            return /^(video|audio)\/mp4/.test(type) && !/hev1|hvc1|hevc|av01|vp09/i.test(type);
        }
        addSourceBuffer(type) {
            if (!FakeMS.isTypeSupported(type)) throw dom('the type is not supported', 'NotSupportedError');
            if (this.readyState !== 'open') throw dom('the MediaSource is not open', 'InvalidStateError');
            var sb = new FakeSB(this, type);
            this.sourceBuffers.push(sb);
            st(this.el).sb = sb;
            return sb;
        }
        removeSourceBuffer(sb) { this.sourceBuffers = this.sourceBuffers.filter(function (x) { return x !== sb; }); }
        endOfStream() { this.readyState = 'ended'; }
    }
    class FakeMMS extends FakeMS {
        constructor() {
            super();
            this.streaming = false;
        }
    }

    // ---- SourceBuffer
    class FakeSB extends EventTarget {
        constructor(ms, type) {
            super();
            this.ms = ms;
            this.type = type;
            this.updating = false;
            this.mode = 'segments';
            this.timestampOffset = 0;
            this.tracks = {};
            this.rest = new Uint8Array(0);
            this.moof = null;
        }
        get buffered() { return timeRanges(sbRanges(this)); }
    }
    FakeSB.prototype.done = function (error) {
        var el = this.ms.el;
        this.updating = false;
        if (error) { this.dispatchEvent(new Event('error')); }
        else this.dispatchEvent(new Event('update'));
        this.dispatchEvent(new Event('updateend'));
        if (el) refresh(el);
    };
    FakeSB.prototype.appendBuffer = function (data) {
        if (this.updating) throw dom('an operation is in progress', 'InvalidStateError');
        if (this.ms.readyState !== 'open') throw dom('the MediaSource is not open', 'InvalidStateError');
        if (rec.failNext === 'quota') { rec.failNext = ''; throw dom('the buffer is full', 'QuotaExceededError'); }
        var u8 = data instanceof Uint8Array ? data : new Uint8Array(data.buffer || data, data.byteOffset || 0, data.byteLength);
        var s = st(this.ms.el);
        rec.appends.push({ at: Date.now(), bytes: u8.length, ct: s.ct });
        this.updating = true;
        this.dispatchEvent(new Event('updatestart'));
        var self = this;
        setTimeout(function () { self.process(u8); }, 2);
    };
    FakeSB.prototype.process = function (data) {
        var self = this, all = new Uint8Array(this.rest.length + data.length), bad = false;
        all.set(this.rest, 0);
        all.set(data, this.rest.length);
        var boxes = children(all, 0, all.length), used = 0;
        boxes.forEach(function (b) {
            if (b.type === 'moov') {
                readInit(all, b, self);
                rec.inits++;
                if (self.ms.el) st(self.ms.el).meta = true;
            } else if (b.type === 'moof') {
                self.moof = readFragment(all, b, self);     // read now: its mdat may come in a later append
            } else if (b.type === 'mdat' && self.moof) {
                var parts = self.moof, fake = String.fromCharCode(all[b.start], all[b.start + 1], all[b.start + 2], all[b.start + 3]) === 'FAKE';
                parts.forEach(function (f) {
                    var track = self.tracks[f.id];
                    track.frags = track.frags.filter(function (g) { return g.t + g.d <= f.t + 0.001 || g.t >= f.t + f.d - 0.001; });
                    track.frags.push({ t: f.t, d: f.d, key: f.key });
                });
                // marker: the fake ffmpeg's one character in every fragment (which run, or which stream, it came from)
                rec.fragments.push({ at: Date.now(), tracks: parts.map(function (f) { return f.id; }), t: parts[0] && parts[0].t,
                                     key: parts.some(function (f) { return f.key && self.tracks[f.id].kind === 'vide'; }),
                                     marker: fake ? String.fromCharCode(all[b.start + 5]) : '' });
                self.moof = null;
            }
            used = b.end;
        });
        // a box that is not whole yet waits for the next append; one that cannot be a box is an error
        this.rest = all.slice(used);
        if (this.rest.length >= 8) {
            var size = new DataView(this.rest.buffer, this.rest.byteOffset, this.rest.byteLength).getUint32(0);
            if (size !== 0 && size !== 1 && size < 8) bad = true;
        }
        var max = 0;
        for (var id in this.tracks) this.tracks[id].frags.forEach(function (f) { max = Math.max(max, f.t + f.d); });
        if (isNaN(this.ms.duration) || max > this.ms.duration) this.ms.duration = max;
        this.done(bad);
    };
    FakeSB.prototype.remove = function (start, end) {
        if (this.updating) throw dom('an operation is in progress', 'InvalidStateError');
        if (!(start >= 0 && end > start)) throw new TypeError('bad range');
        var entry = { at: Date.now(), start: start, end: end, ct: this.ms.el ? st(this.ms.el).ct : 0,
                      before: sbRanges(this).map(function (r) { return [r.start, r.end]; }), after: null };
        rec.removes.push(entry);
        this.updating = true;
        this.dispatchEvent(new Event('updatestart'));
        var self = this;
        setTimeout(function () {
            var dur = self.ms.duration || 0;
            for (var id in self.tracks) {
                var track = self.tracks[id], stop = dur;
                // the standard: the removal runs on to the first random access point at or after `end`, else to the duration
                track.frags.forEach(function (f) { if (f.key && f.t >= end - 1e-6 && f.t < stop) stop = f.t; });
                track.frags = track.frags.filter(function (f) { return !(f.t >= start - 1e-6 && f.t < stop); });
            }
            entry.after = sbRanges(self).map(function (r) { return [r.start, r.end]; });
            self.done(false);
        }, 2);
    };
    FakeSB.prototype.abort = function () { rec.aborts++; this.rest = new Uint8Array(0); this.moof = null; };

    // ---- the media element
    function rangeAt(s, t) {
        var all = s.sb ? sbRanges(s.sb) : [];
        for (var i = 0; i < all.length; i++) if (t >= all[i].start - 1e-6 && t <= all[i].end + 1e-6) return all[i];
        return null;
    }
    // Recomputes readyState from what is buffered at the playhead, and fires the events that follow from it.
    function refresh(el) {
        var s = st(el);
        if (!s.sb) return;
        if (s.meta && !s.metaFired) {
            s.metaFired = true;
            s.ready = Math.max(s.ready, 1);
            queue(el, 'loadedmetadata');
            if (videoWidth(s)) queue(el, 'resize');
        }
        var r = s.meta ? rangeAt(s, s.ct) : null;
        // a stalled element needs a little more data than a playing one to carry on, as a browser does
        var ready = !s.meta ? 0 : !r ? 1 : (r.end - s.ct > (s.ready >= 3 ? 0.001 : 0.35) ? 4 : 2);
        var was = s.ready;
        s.ready = ready;
        if (was < 2 && ready >= 2) queue(el, 'loadeddata');
        if (was < 3 && ready >= 3) {
            queue(el, 'canplay');
            if (!s.paused) queue(el, 'playing');
        }
        if (was >= 3 && ready < 3 && !s.paused && !s.seeking) queue(el, 'waiting');
    }
    var proto = HTMLMediaElement.prototype;
    var origSrc = Object.getOwnPropertyDescriptor(proto, 'src');
    var urls = {}, urlSeq = 0;
    var origCreate = URL.createObjectURL.bind(URL), origRevoke = URL.revokeObjectURL.bind(URL);
    // Only a fake MediaSource gets a fake URL: the page's worker clock makes a real blob URL through the same call.
    URL.createObjectURL = function (obj) {
        if (obj instanceof FakeMS) { var u = 'blob:fake-media/' + (++urlSeq); urls[u] = obj; rec.urls.push(u); return u; }
        return origCreate(obj);
    };
    URL.revokeObjectURL = function (u) {
        if (typeof u === 'string' && u.indexOf('blob:fake-media/') === 0) { rec.revoked.push(u); return; }
        return origRevoke(u);
    };
    Object.defineProperty(proto, 'src', {
        configurable: true,
        get: function () { return st(this).src || ''; },
        set: function (value) {
            var el = this, s = st(el), ms = urls[value];
            rec.srcs.push(String(value));
            if (!ms) { if (origSrc && origSrc.set) origSrc.set.call(el, value); return; }
            s.src = value;
            s.ms = ms;
            ms.el = el;
            live.add(el);
            setTimeout(function () {
                if (s.ms !== ms || q.get('__open') === 'never') return;
                // a ManagedMediaSource opens only for an element that has disableRemotePlayback set
                if (ms instanceof FakeMMS && !el.disableRemotePlayback) return;
                ms.readyState = 'open';
                rec.opens++;
                ms.dispatchEvent(new Event('sourceopen'));
                if (ms instanceof FakeMMS) { ms.streaming = true; ms.dispatchEvent(new Event('startstreaming')); }
            }, 5);
        },
    });
    proto.load = function () {
        var el = this, s = st(el);
        rec.loads++;
        if (s.ms) { s.ms.readyState = 'closed'; s.ms.dispatchEvent(new Event('sourceclose')); s.ms.el = null; }
        live.delete(el);
        s.ms = null; s.sb = null; s.src = ''; s.paused = true; s.ct = 0; s.ready = 0; s.meta = false; s.metaFired = false; s.seeking = false;
    };
    proto.play = function () {
        var el = this, s = st(el);
        var entry = { at: Date.now(), muted: s.muted, volume: el.volume, gesture: !!window.__rec.gesture, result: 'ok' };
        rec.plays.push(entry);
        if ((autoplay === 'blocked' && !window.__rec.gesture) || (autoplay === 'muted' && !s.muted && !window.__rec.gesture)) {
            entry.result = 'NotAllowedError';
            return Promise.reject(dom('play() is blocked by the autoplay policy', 'NotAllowedError'));
        }
        if (s.paused) {
            s.paused = false;
            queue(el, 'play');
            if (s.ready >= 3) queue(el, 'playing');
        }
        return Promise.resolve();
    };
    proto.pause = function () {
        var s = st(this);
        rec.pauses.push({ at: Date.now(), wasPaused: s.paused });
        if (!s.paused) { s.paused = true; queue(this, 'pause'); }
    };
    function prop(name, get, set, target) { Object.defineProperty(target || proto, name, { configurable: true, get: get, set: set }); }
    prop('muted', function () { return st(this).muted; }, function (value) {
        var s = st(this), was = s.muted;
        s.muted = !!value;
        if (!was || s.muted) return;
        var entry = { at: Date.now(), gesture: !!window.__rec.gesture, wasPlaying: !s.paused, paused: false };
        rec.unmutes.push(entry);
        // the browser refuses an unmute that comes without a gesture, and pauses the element
        if (!s.paused && (rec.refuseUnmute || (!window.__rec.gesture && autoplay !== 'allowed'))) { s.paused = true; entry.paused = true; queue(this, 'pause'); }
    });
    prop('paused', function () { return st(this).paused; });
    prop('currentTime', function () { return st(this).ct; }, function (value) {
        var el = this, s = st(el);
        rec.seeks.push({ at: Date.now(), from: s.ct, to: Number(value), paused: s.paused, hidden: document.hidden, buffered: ranges(s) });
        s.ct = Number(value);
        s.seeking = true;
        queue(el, 'seeking');
        setTimeout(function () { s.seeking = false; queue(el, 'seeked'); refresh(el); }, 5);
    });
    prop('seeking', function () { return st(this).seeking; });
    prop('readyState', function () { return st(this).ready; });
    prop('buffered', function () { return timeRanges(ranges(st(this)).map(function (r) { return { start: r[0], end: r[1] }; })); });
    prop('videoWidth', function () { return videoWidth(st(this)); }, undefined, HTMLVideoElement.prototype);
    prop('videoHeight', function () { return videoHeight(st(this)); }, undefined, HTMLVideoElement.prototype);
    prop('duration', function () { var s = st(this); return s.ms ? s.ms.duration : NaN; });
    prop('error', function () { return st(this).error || null; });
    prop('ended', function () { return false; });
    // media time passes: the playhead moves while the element plays and has data, and waits at the end of it
    setInterval(function () {
        var t = performance.now();
        live.forEach(function (el) {
            var s = st(el), dt = (t - s.last) / 1000;
            s.last = t;
            if (s.paused || s.seeking || s.ready < 3 || rec.hold) return;
            var r = rangeAt(s, s.ct);
            if (!r) return refresh(el);
            s.ct = Math.min(r.end, s.ct + dt * RATE);
            if (t - s.tu > 250) { s.tu = t; fire(el, 'timeupdate'); }
            refresh(el);
        });
    }, 40);

    var realFetch = window.fetch;
    window.fetch = function (url, opts) {
        var path = String(url), entry = path.indexOf('/api/stream/media') === 0 ? [Date.now(), path, 0] : null;
        if (entry) rec.fetches.push(entry);
        var promise = realFetch.apply(this, arguments);
        if (entry) promise.then(function (res) { entry[2] = res.status; }, function () { entry[2] = -1; });
        return promise;
    };

    // ---- what exists
    if (q.get('__mse') !== 'none') {
        Object.defineProperty(window, 'MediaSource', { configurable: true, writable: true, value: mseMode === 'mms' ? undefined : FakeMS });
        Object.defineProperty(window, 'ManagedMediaSource', { configurable: true, writable: true, value: mseMode === 'mms' || mseMode === 'both' ? FakeMMS : undefined });
    } else {
        Object.defineProperty(window, 'MediaSource', { configurable: true, writable: true, value: undefined });
        Object.defineProperty(window, 'ManagedMediaSource', { configurable: true, writable: true, value: undefined });
    }
    try {
        Object.defineProperty(navigator, 'userActivation', {
            configurable: true,
            get: function () { return { get isActive() { return !!window.__rec.gesture; }, get hasBeenActive() { return !!window.__rec.gesture; } }; },
        });
    } catch (e) { /* the real one stays */ }
})();
