import assert from "node:assert/strict";
import test from "node:test";
import type { Terminal } from "@earendil-works/pi-tui";
import type { WorkerRecord } from "../src/types.js";
import {
  getInteractiveModeAccess,
  stopWithoutTerminalEffects,
  syncWorkerConfigurationFromSession,
  type InteractiveModeAccess,
} from "../src/runtime.js";

function recordingTerminal(events: string[]): Terminal {
  return {
    start() { events.push("start"); },
    stop() { events.push("stop"); },
    async drainInput() { events.push("drainInput"); },
    write() { events.push("write"); },
    get columns() { return 120; },
    get rows() { return 40; },
    get kittyProtocolActive() { return false; },
    moveBy() { events.push("moveBy"); },
    hideCursor() { events.push("hideCursor"); },
    showCursor() { events.push("showCursor"); },
    clearLine() { events.push("clearLine"); },
    clearFromCursor() { events.push("clearFromCursor"); },
    clearScreen() { events.push("clearScreen"); },
    setTitle() { events.push("setTitle"); },
    setProgress() { events.push("setProgress"); },
  };
}

function fakeAccess(terminal: Terminal): InteractiveModeAccess {
  return {
    ui: {
      terminal,
      start() {},
      stop() {},
      requestRender() {},
    },
    async run() {},
    stop() {},
  };
}

test("worker records use the model and thinking level accepted by the child session", () => {
  const worker = {
    model: { provider: "ollama", id: "requested" },
    thinkingLevel: "medium",
  } as WorkerRecord;

  syncWorkerConfigurationFromSession(worker, {
    model: { provider: "ollama", id: "actual" },
    thinkingLevel: "off",
  } as Parameters<typeof syncWorkerConfigurationFromSession>[1]);

  assert.deepEqual(worker.model, { provider: "ollama", id: "actual" });
  assert.equal(worker.thinkingLevel, "off");
});

test("the private InteractiveMode adapter validates the capabilities it uses", () => {
  const access = fakeAccess(recordingTerminal([]));
  assert.equal(getInteractiveModeAccess(access), access);
  assert.throws(
    () => getInteractiveModeAccess({ ...access, ui: { terminal: access.ui.terminal } }),
    /InteractiveMode private TUI is incompatible/,
  );
});

test("inactive disposal stays terminal-silent across a renderer replacement", () => {
  const events: string[] = [];
  const terminal = recordingTerminal(events);
  let renderer = fakeAccess(terminal).ui;
  const ui = new Proxy({} as InteractiveModeAccess["ui"], {
    get: (_target, property) => Reflect.get(renderer, property, renderer),
    set: (_target, property, value) => Reflect.set(renderer, property, value, renderer),
  });
  const access: InteractiveModeAccess = {
    ui,
    async run() {},
    stop() {
      renderer.terminal.write("old renderer stop");
      renderer = fakeAccess(renderer.terminal).ui;
      renderer.terminal.setTitle("replacement renderer");
      renderer.terminal.stop();
    },
  };

  stopWithoutTerminalEffects(access);

  assert.deepEqual(events, []);
  assert.equal(renderer.terminal, terminal);
});

test("inactive disposal restores the terminal when InteractiveMode.stop throws", () => {
  const events: string[] = [];
  const terminal = recordingTerminal(events);
  const access = fakeAccess(terminal);
  access.stop = () => {
    access.ui.terminal.write("muted");
    throw new Error("stop failed");
  };

  assert.throws(() => stopWithoutTerminalEffects(access), /stop failed/);
  assert.deepEqual(events, []);
  assert.equal(access.ui.terminal, terminal);
});
