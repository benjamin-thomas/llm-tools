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
// [key, name, span, brand colour, show the amount left]. The brand colour
// says whose gauge it is. Hyper's card also names what is left, in the Hyper
// Credits (HC) it bills in. Claude's 5h window is left out on purpose: it
// opens on the first message, so it has no pace to keep.
const SHOWN = [
    ['claude-week', 'Claude', 'week', '#d97757', false],
    ['hyper', 'Hyper', 'day', '#ff60ff', true],
    ['mistral', 'Mistral', 'month', '#fa520f', false],
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
// How much of the bar's height the used strip takes ('strip' only).
const STRIP = 0.18;
// Barely rounded: full pill ends cropped the strip into a sliver.
const RADIUS = 2;

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

// The bar, on a faint track within barely rounded corners; see STYLE.
function paintGauge(area, gauge) {
    const cr = area.get_context();
    const [width, height] = area.get_surface_size();
    const time = gauge ? share(gauge.elapsed) : null;
    const used = gauge ? share(gauge.used) : null;
    const [red, green, blue] = rgb(usedColour(gauge));
    roundedRect(cr, 0, 0, width, height, RADIUS);
    cr.clip();
    cr.setSourceRGBA(1, 1, 1, 0.10);
    cr.paint();
    if (STYLE === 'strip') {
        if (time !== null) {
            cr.setSourceRGBA(1, 1, 1, 0.38);
            cr.rectangle(0, 0, width * time, height);
            cr.fill();
        }
        if (used !== null) {
            const strip = Math.max(2, Math.floor(height * STRIP));
            cr.setSourceRGBA(red, green, blue, 1);
            cr.rectangle(0, height - strip, width * used, strip);
            cr.fill();
        }
    } else {
        if (used !== null) {
            cr.setSourceRGBA(red, green, blue, 1);
            cr.rectangle(0, 0, width * used, height);
            cr.fill();
        }
        // Drawn last, so it reads over the fill; inset by half its width so
        // the whole line lands inside the bar.
        if (time !== null && time > 0) {
            const line = 1;
            cr.setSourceRGBA(...FRAME_RGBA);
            cr.setLineWidth(line);
            roundedRect(cr, line / 2, line / 2, Math.max(line, width * time - line), height - line, RADIUS);
            cr.stroke();
        }
    }
    cr.$dispose();
}

// One card in the open menu: whose and how it stands, the bar, then both
// shares in words and when the window resets.
const Card = GObject.registerClass(
class Card extends PopupMenu.PopupBaseMenuItem {
    _init(name, span, brand, withDetail) {
        super._init({ reactive: false, can_focus: false });
        this._gauge = null;
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
        this.add_child(box);
    }

    update(gauge) {
        this._gauge = gauge;
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
});

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init() {
        super._init(0.0, 'Quota gauges');
        this._gauges = {};
        this._busy = false;

        const strip = new St.BoxLayout({ style_class: 'panel-status-menu-box qg-strip' });
        this._bars = {};
        for (const [key, name, , brand] of SHOWN) {
            strip.add_child(new St.Label({
                text: name[0],
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
        for (const [key, name, span, brand, withDetail] of SHOWN) {
            const card = new Card(name, span, brand, withDetail);
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
        for (const [key] of SHOWN) {
            this._bars[key].queue_repaint();
            this._cards[key].update(this._gauges[key]);
        }
        const at = GLib.DateTime.new_now_local().format('%H:%M');
        this._refreshItem.label.text = `Refresh now · updated ${at}`;
    }

    _fail(message) {
        this._gauges = {};
        for (const [key] of SHOWN) {
            this._bars[key].queue_repaint();
            this._cards[key].update({ key, used: null, tone: 'unknown', problem: message });
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
