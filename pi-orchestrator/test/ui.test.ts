import assert from "node:assert/strict";
import test from "node:test";
import type { Theme } from "@earendil-works/pi-coding-agent";
import {
  OrchestratorWidget,
  preferredSwitcherIndex,
  switchTargetForKey,
} from "../src/ui.js";
import { ORCHESTRATOR_ID, type OrchestratorSnapshot } from "../src/types.js";

const plainTheme = {
  fg: (_color: string, text: string) => text,
} as unknown as Theme;

test("a renamed worker displays its model and unread response badge", () => {
  // Arrange
  const snapshot: OrchestratorSnapshot = {
    active: true,
    focusedId: "worker-1",
    coordinator: {
      id: ORCHESTRATOR_ID,
      name: "orchestrator",
      cwd: "/repo",
      thinkingLevel: "high",
      activity: "idle",
      sessionId: "coordinator-session",
      lastActivityAt: 1,
      focused: false,
      key: "0",
    },
    workers: [{
      id: "worker-1",
      slot: 6,
      key: "2",
      name: "kimi-k3",
      cwd: "/repo",
      model: { provider: "kimi-coding", id: "k3" },
      thinkingLevel: "high",
      activity: "idle",
      createdAt: 1,
      lastActivityAt: 1,
      readCursor: null,
      unreadCount: 2,
      focused: true,
    }],
    scopedModels: [{ provider: "kimi-coding", id: "k3" }],
  };
  const widget = new OrchestratorWidget(plainTheme, () => snapshot, () => {});

  // Act
  const rendered = widget.render(120).join("\n");

  // Assert
  assert.match(rendered, /2 kimi-k3 \(k3 · high\)/);
  assert.match(rendered, /\+2/);
});

test("the status widget wraps so every worker shortcut remains visible", () => {
  const workers = Array.from({ length: 6 }, (_, index) => ({
    id: `worker-${index}`,
    slot: 5 + index,
    key: `${index + 1}`,
    name: `model-${index}`,
    cwd: "/repo",
    model: { provider: "provider", id: `model-${index}` },
    thinkingLevel: "high" as const,
    activity: "idle" as const,
    createdAt: 1,
    lastActivityAt: 1,
    readCursor: null,
    unreadCount: 0,
    focused: false,
  }));
  const snapshot: OrchestratorSnapshot = {
    active: true,
    focusedId: ORCHESTRATOR_ID,
    coordinator: {
      id: ORCHESTRATOR_ID,
      name: "orchestrator",
      cwd: "/repo",
      thinkingLevel: "high",
      activity: "idle",
      sessionId: "coordinator-session",
      lastActivityAt: 1,
      focused: true,
      key: "0",
    },
    workers,
    scopedModels: [],
  };
  const widget = new OrchestratorWidget(plainTheme, () => snapshot, () => {});

  const lines = widget.render(42);
  const rendered = lines.join("\n");

  assert.ok(lines.length > 1, "narrow displays should wrap onto multiple rows");
  for (let key = 0; key <= 6; key++) assert.match(rendered, new RegExp(`\\b${key}\\b`));
});

test("switcher digits target the orchestrator and stable worker slots", () => {
  const snapshot: OrchestratorSnapshot = {
    active: true,
    focusedId: ORCHESTRATOR_ID,
    workers: [
      {
        id: "worker-1",
        slot: 5,
        key: "1",
        name: "one",
        cwd: "/repo",
        model: { provider: "provider", id: "one" },
        thinkingLevel: "off",
        activity: "idle",
        createdAt: 1,
        lastActivityAt: 1,
        readCursor: null,
        unreadCount: 0,
        focused: false,
      },
      {
        id: "worker-3",
        slot: 7,
        key: "3",
        name: "three",
        cwd: "/repo",
        model: { provider: "provider", id: "three" },
        thinkingLevel: "off",
        activity: "idle",
        createdAt: 1,
        lastActivityAt: 1,
        readCursor: null,
        unreadCount: 0,
        focused: false,
      },
    ],
    scopedModels: [],
  };

  assert.equal(switchTargetForKey("0", snapshot), ORCHESTRATOR_ID);
  assert.equal(switchTargetForKey("1", snapshot), "worker-1");
  assert.equal(switchTargetForKey("3", snapshot), "worker-3");
  assert.equal(switchTargetForKey("2", snapshot), undefined);
  assert.equal(switchTargetForKey("x", snapshot), undefined);
  assert.equal(preferredSwitcherIndex(snapshot, "worker-3"), 2);
  assert.equal(preferredSwitcherIndex(snapshot, "closed-worker"), 1);
  assert.equal(
    preferredSwitcherIndex({ ...snapshot, focusedId: "worker-3" }, "worker-1"),
    0,
    "workers should always preselect the orchestrator",
  );
});
