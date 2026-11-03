// Subscription meters in the top bar, the way TopHat shows CPU and memory.
//
// Each subscription is one bar showing two shares: how much of the window has
// gone by (time) and how much of the allowance is spent (used, in a colour
// that says how the two compare). By default used fills the bar and time is a
// soft frame around what could be spent by now: fill the frame without
// spilling past it, and level at the reset is the whole allowance used, on
// time. STYLE switches to the other layout. A click opens one card per
// subscription with both shares spelled out and when the window resets.
//
// Claude's card also has a small bar for its 5h window, in the menu only: the
// top bar stays one bar per subscription. That window opens on the first
// message, so it has no pace to keep and its colour goes by how full it is
// alone (grey until it starts to matter). A faint mark still shows where an
// even pace through the 5h would be: a fill past it is a session spending too
// fast.
//
// The reading and its wording come from Python: this runs the installed
// `quota-gauges --json` once a minute and lays it out. It never touches the
// network or a credential itself.
//
// Written for GNOME Shell 42 (legacy imports). GNOME 45 moved extensions to ES
// modules, so an upgrade past 44 means porting this file.

const { Clutter, Gio, GLib, GObject, St } = imports.gi;
const Main = imports.ui.main;
const PanelMenu = imports.ui.panelMenu;
const PopupMenu = imports.ui.popupMenu;

// The copy `quota-gauges install` puts out of reach of sandboxed agents.
const COMMAND = [GLib.build_filenamev([GLib.get_home_dir(), '.local', 'lib', 'quota-gauges', 'quota-gauges']), '--json'];
const INTERVAL_SECONDS = 60;
// [key, initial, name, span, brand colour, show the amount left, 5h key].
// The brand colour says whose gauge it is. Hyper's card also names what is
// left, in the Hyper Credits (HC) it bills in. Codex goes by O (OpenAI): C is
// Claude's, and X would read as xAI. Its Pro Lite plan has no 5h window.
const SHOWN = [
    ['claude-week', 'C', 'Claude', 'week', '#d97757', false, 'claude-5h'],
    ['codex-week', 'O', 'Codex', 'week', '#c0c4cc', false, null],
    ['hyper', 'H', 'Hyper', 'day', '#ff60ff', true, null],
    ['mistral', 'M', 'Mistral', 'month', '#fa520f', false, null],
];
// The tones quota-gauges hands out (quota_gauges/gauge.py: tone), lightened
// to read on a dark bar. Time is plain light grey, as it judges nothing.
const COLOURS = {
    under: '#78aeed',
    ok: '#57e389',
    warn: '#f8e45c',
    over: '#ff7b63',
    stale: '#9a9996',
    unknown: '#9a9996',
};
// How a bar shows the two shares:
//   'frame': used fills the bar in its tone; time is a soft frame around the
//            share that could be spent by now. Fill the frame, do not spill.
//   'strip': time fills the bar in light grey; used is a thin strip along its
//            foot that should catch the grey up.
const STYLE = 'frame';
// The frame: a soft grey rather than white, which pulled the eye too hard.
const FRAME_RGBA = [0.78, 0.78, 0.76, 0.55];
// Once used spills past time, the soft grey vanishes into the fill: time is
// then a dark notch across it instead. Only the edge, as a whole dark frame
// made the fill look shorter than the spill.
const NOTCH_RGBA = [0.1, 0.1, 0.1, 0.9];
const NOTCH_WIDTH = 2;
// How much of the bar's height the used strip takes ('strip' only).
const STRIP = 0.18;
// Barely rounded: full pill ends cropped the strip into a sliver.
const RADIUS = 2;
// The 5h fill while it is merely 'ok': it judges no pace, so it stays quiet.
const FIVE_QUIET = '#c8c8c4';
// The even-pace mark on the 5h strip, while the fill is still behind it:
// one pixel wide, the strip's height, never spilling out of it. The soft grey
// of the week's pace frame: white drew the eye all the time.
const MARK_RGBA = FRAME_RGBA;
// Once the fill has passed it: a faint shade, to be found, not seen.
const MARK_PASSED_RGBA = [0.1, 0.1, 0.1, 0.35];
const MARK_WIDTH = 1;

function rgb(hex) {
    return [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
}

function share(value) {
    return typeof value === 'number' ? Math.min(1, Math.max(0, value)) : null;
}

function usedColour(gauge) {
    return COLOURS[gauge ? gauge.tone : 'unknown'] || COLOURS.unknown;
}

function percent(value) {
    return typeof value === 'number' ? `${Math.round(value * 100)}%` : '–';
}

function roundedRect(cr, x, y, width, height, radius) {
    const r = Math.min(radius, height / 2, width / 2);
    cr.newSubPath();
    cr.arc(x + width - r, y + r, r, -Math.PI / 2, 0);
    cr.arc(x + width - r, y + height - r, r, 0, Math.PI / 2);
    cr.arc(x + r, y + height - r, r, Math.PI / 2, Math.PI);
    cr.arc(x + r, y + r, r, Math.PI, 3 * Math.PI / 2);
    cr.closePath();
}

function fiveColour(gauge) {
    return gauge && gauge.tone === 'ok' ? FIVE_QUIET : usedColour(gauge);
}

// A faint track within barely rounded corners, clipped to; undo with restore().
function track(cr, x, y, width, height) {
    cr.save();
    roundedRect(cr, x, y, width, height, RADIUS);
    cr.clip();
    cr.setSourceRGBA(1, 1, 1, 0.10);
    cr.paint();
}

// A window with a pace to keep, in the given rectangle; see STYLE.
function paintPaced(cr, x, y, width, height, gauge) {
    const time = gauge ? share(gauge.elapsed) : null;
    const used = gauge ? share(gauge.used) : null;
    const [red, green, blue] = rgb(usedColour(gauge));
    track(cr, x, y, width, height);
    if (STYLE === 'strip') {
        if (time !== null) {
            cr.setSourceRGBA(1, 1, 1, 0.38);
            cr.rectangle(x, y, width * time, height);
            cr.fill();
        }
        if (used !== null) {
            const strip = Math.max(2, Math.floor(height * STRIP));
            cr.setSourceRGBA(red, green, blue, 1);
            cr.rectangle(x, y + height - strip, width * used, strip);
            cr.fill();
        }
    } else {
        if (used !== null) {
            cr.setSourceRGBA(red, green, blue, 1);
            cr.rectangle(x, y, width * used, height);
            cr.fill();
        }
        // Drawn last, so it reads over the fill.
        if (time !== null && used !== null && used > time) {
            cr.setSourceRGBA(...NOTCH_RGBA);
            cr.rectangle(x + width * time - NOTCH_WIDTH / 2, y, NOTCH_WIDTH, height);
            cr.fill();
        } else if (time !== null && time > 0) {
            // Inset by half its width so the whole line lands inside the bar.
            const line = 1;
            cr.setSourceRGBA(...FRAME_RGBA);
            cr.setLineWidth(line);
            roundedRect(cr, x + line / 2, y + line / 2, Math.max(line, width * time - line), height - line, RADIUS);
            cr.stroke();
        }
    }
    cr.restore();
}

// The 5h window: its fill, and a mark where an even pace would be. Too thin
// for a frame, so the mark is soft grey behind the fill and a faint shade once
// passed: a mark is there to be looked for.
function paintFive(cr, x, y, width, height, gauge) {
    const used = gauge ? share(gauge.used) : null;
    // No reset means no session open: the window has not started.
    const time = gauge && gauge.resets_at ? share(gauge.elapsed) : null;
    track(cr, x, y, width, height);
    if (used !== null) {
        cr.setSourceRGBA(...rgb(fiveColour(gauge)), 1);
        cr.rectangle(x, y, width * used, height);
        cr.fill();
    }
    if (time !== null && time > 0) {
        // On whole pixels, so one pixel wide stays sharp.
        const at = Math.min(x + width - MARK_WIDTH, Math.max(x, Math.round(x + width * time)));
        cr.setSourceRGBA(...(used !== null && used > time ? MARK_PASSED_RGBA : MARK_RGBA));
        cr.rectangle(at, y, MARK_WIDTH, height);
        cr.fill();
    }
    cr.restore();
}

function paintGauge(area, gauge) {
    const cr = area.get_context();
    const [width, height] = area.get_surface_size();
    paintPaced(cr, 0, 0, width, height, gauge);
    cr.$dispose();
}

function paintFiveArea(area, five) {
    const cr = area.get_context();
    const [width, height] = area.get_surface_size();
    paintFive(cr, 0, 0, width, height, five);
    cr.$dispose();
}

// One card in the open menu: whose and how it stands, the bar, then both
// shares in words and when the window resets. With a 5h window, a small line
// under it says the same of that window.
const Card = GObject.registerClass(
class Card extends PopupMenu.PopupBaseMenuItem {
    _init(name, span, brand, withDetail, withFive) {
        super._init({ reactive: false, can_focus: false });
        this._gauge = null;
        this._five = null;
        this._span = span;
        this._withDetail = withDetail;
        const box = new St.BoxLayout({ vertical: true, style_class: 'qg-card', x_expand: true });

        const head = new St.BoxLayout({ x_expand: true });
        head.add_child(new St.Widget({
            style_class: 'qg-dot',
            style: `background-color: ${brand};`,
            y_align: Clutter.ActorAlign.CENTER,
        }));
        head.add_child(new St.Label({ text: name, style_class: 'qg-card-name', y_align: Clutter.ActorAlign.CENTER }));
        head.add_child(new St.Label({ text: span, style_class: 'qg-card-span', x_expand: true, y_align: Clutter.ActorAlign.CENTER }));
        this._status = new St.Label({ style_class: 'qg-status', y_align: Clutter.ActorAlign.CENTER });
        head.add_child(this._status);
        box.add_child(head);

        this._bar = new St.DrawingArea({ style_class: 'qg-card-bar', x_expand: true });
        this._bar.connect('repaint', area => paintGauge(area, this._gauge));
        box.add_child(this._bar);

        const foot = new St.BoxLayout({ x_expand: true });
        this._used = new St.Label({ style_class: 'qg-used', y_align: Clutter.ActorAlign.CENTER });
        this._time = new St.Label({ style_class: 'qg-dim', x_expand: true, y_align: Clutter.ActorAlign.CENTER });
        this._resets = new St.Label({ style_class: 'qg-dim', y_align: Clutter.ActorAlign.CENTER });
        [this._used, this._time, this._resets].forEach(label => foot.add_child(label));
        box.add_child(foot);

        this._fiveLine = null;
        if (withFive) {
            const line = new St.BoxLayout({ x_expand: true, style_class: 'qg-five' });
            line.add_child(new St.Label({ text: '5h', style_class: 'qg-five-name', y_align: Clutter.ActorAlign.CENTER }));
            this._fiveBar = new St.DrawingArea({ style_class: 'qg-five-bar', y_align: Clutter.ActorAlign.CENTER });
            this._fiveBar.connect('repaint', area => paintFiveArea(area, this._five));
            line.add_child(this._fiveBar);
            this._fiveUsed = new St.Label({ style_class: 'qg-five-text', y_align: Clutter.ActorAlign.CENTER });
            this._fiveTime = new St.Label({ style_class: 'qg-dim qg-five-text', x_expand: true, y_align: Clutter.ActorAlign.CENTER });
            this._fiveResets = new St.Label({ style_class: 'qg-dim qg-five-text', y_align: Clutter.ActorAlign.CENTER });
            [this._fiveUsed, this._fiveTime, this._fiveResets].forEach(label => line.add_child(label));
            box.add_child(line);
            this._fiveLine = line;
        }
        this.add_child(box);
    }

    update(gauge, five) {
        this._gauge = gauge;
        if (this._fiveLine)
            this._updateFive(five);
        const said = (gauge && gauge.phrases) || {};
        const reading = gauge && typeof gauge.used === 'number';
        const detail = this._withDetail && reading && gauge.detail ? ` · ${gauge.detail}` : '';
        this._status.text = reading ? `${said.status || ''}${detail}` : (gauge && gauge.problem) || 'no reading';
        this._status.style = `color: ${usedColour(gauge)};`;
        this._used.text = reading ? `${percent(gauge.used)} used` : '';
        this._used.style = `color: ${usedColour(gauge)};`;
        this._time.text = gauge && typeof gauge.elapsed === 'number'
            ? ` · ${percent(gauge.elapsed)} of the ${this._span} gone` : '';
        this._resets.text = said.resets_short || '';
        this._bar.queue_repaint();
    }

    _updateFive(five) {
        this._five = five || null;
        const reading = five && typeof five.used === 'number';
        const open = reading && five.resets_at;
        const loud = open && (five.tone === 'warn' || five.tone === 'over');
        if (!reading)
            this._fiveUsed.text = (five && five.problem) || 'no reading';
        else
            this._fiveUsed.text = open ? `${percent(five.used)} used` : 'no session';
        this._fiveUsed.style = loud ? `color: ${fiveColour(five)}; font-weight: bold;` : 'color: #9a9996;';
        this._fiveTime.text = open && typeof five.elapsed === 'number' ? ` · ${percent(five.elapsed)} gone` : '';
        this._fiveResets.text = open ? (five.phrases && five.phrases.resets_short) || '' : '';
        this._fiveBar.queue_repaint();
    }
});

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init() {
        super._init(0.0, 'Quota gauges');
        this._gauges = {};
        this._busy = false;

        const strip = new St.BoxLayout({ style_class: 'panel-status-menu-box qg-strip' });
        this._bars = {};
        for (const [key, initial, , , brand] of SHOWN) {
            strip.add_child(new St.Label({
                text: initial,
                style_class: 'qg-initial',
                style: `color: ${brand};`,
                y_align: Clutter.ActorAlign.CENTER,
            }));
            const bar = new St.DrawingArea({ style_class: 'qg-bar', y_align: Clutter.ActorAlign.CENTER });
            bar.connect('repaint', area => paintGauge(area, this._gauges[key]));
            strip.add_child(bar);
            this._bars[key] = bar;
        }
        this.add_child(strip);

        this._cards = {};
        for (const [key, , name, span, brand, withDetail, fiveKey] of SHOWN) {
            const card = new Card(name, span, brand, withDetail, Boolean(fiveKey));
            this.menu.addMenuItem(card);
            this._cards[key] = card;
        }
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._refreshItem = new PopupMenu.PopupMenuItem('Refresh now');
        this._refreshItem.connect('activate', () => this._refresh());
        this.menu.addMenuItem(this._refreshItem);

        this._refresh();
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, INTERVAL_SECONDS, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _refresh() {
        // Skip, do not queue: a slow endpoint must not stack up runs.
        if (this._busy)
            return;
        this._busy = true;
        let proc;
        try {
            proc = Gio.Subprocess.new(COMMAND,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
        } catch (e) {
            this._busy = false;
            this._fail(`cannot run quota-gauges: ${e.message} (run quota-gauges install)`);
            return;
        }
        proc.communicate_utf8_async(null, null, (p, result) => {
            this._busy = false;
            try {
                const [, out] = p.communicate_utf8_finish(result);
                this._show(JSON.parse(out));
            } catch (e) {
                this._fail(`quota-gauges failed: ${e.message}`);
            }
        });
    }

    _show(gauges) {
        this._gauges = {};
        for (const gauge of gauges)
            this._gauges[gauge.key] = gauge;
        for (const [key, , , , , , fiveKey] of SHOWN) {
            this._bars[key].queue_repaint();
            this._cards[key].update(this._gauges[key], fiveKey ? this._gauges[fiveKey] : null);
        }
        const at = GLib.DateTime.new_now_local().format('%H:%M');
        this._refreshItem.label.text = `Refresh now · updated ${at}`;
    }

    _fail(message) {
        this._gauges = {};
        for (const [key] of SHOWN) {
            this._bars[key].queue_repaint();
            this._cards[key].update({ key, used: null, tone: 'unknown', problem: message }, null);
        }
    }

    destroy() {
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = null;
        }
        super.destroy();
    }
});

class Extension {
    enable() {
        this._indicator = new Indicator();
        // The left box, which is empty here; the right one is full.
        Main.panel.addToStatusArea('quota-gauges', this._indicator, 1, 'left');
    }

    disable() {
        this._indicator.destroy();
        this._indicator = null;
    }
}

function init() {
    return new Extension();
}
