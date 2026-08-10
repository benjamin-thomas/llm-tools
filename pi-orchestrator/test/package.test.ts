import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

interface PackageManifest {
  files?: string[];
  pi?: { skills?: string[] };
}

test("the package ships the native rotating TDD skill", () => {
  const manifest = JSON.parse(readFileSync("package.json", "utf8")) as PackageManifest;
  const skill = readFileSync("skills/rotating-tdd/SKILL.md", "utf8");

  assert.ok(manifest.files?.includes("skills"));
  assert.deepEqual(manifest.pi?.skills, ["./skills"]);
  assert.match(skill, /^---\nname: rotating-tdd\n/m);
  assert.match(skill, /orchestrator/);
  assert.match(skill, /Flexible deliberation mode/);
  assert.match(skill, /Rigid TDD execution mode/);
  assert.match(skill, /tester[\s\S]*implementer[\s\S]*reviewer/);
  assert.doesNotMatch(skill, /`implement`/);
  assert.doesNotMatch(skill, /\bcrew\b/i);
  assert.match(skill, /reviewer-1/);
  assert.match(skill, /strongest objections/);
  assert.match(skill, /Consensus is not mandatory/);
  assert.match(skill, /Reset only idle workers/);
  assert.match(skill, /Progress: ~<percent>%/);
  assert.match(skill, /ETA: <local clock time>/);
  assert.match(skill, /Confidence: <low \| medium \| high>/);
  assert.match(skill, /Next intention:/);
  assert.match(skill, /Proceed to GREEN\? \(y\/N\)/);
  assert.match(skill, /Proceed to REVIEW\? \(y\/N\)/);
  assert.match(skill, /Do not dispatch the next worker/);
  assert.doesNotMatch(skill, /Create this worker setup\? \(y\/N\)/);
  assert.doesNotMatch(skill, /Approve worker setup\? \(y\/N\)/);
  assert.match(skill, /Do not ask for a separate setup approval/);
  assert.match(skill, /first dispatch proposal is also the\s+setup checkpoint/i);
  assert.match(skill, /Never show `default-balanced`/);
  assert.match(skill, /concrete `provider\/model`/);
  assert.match(skill, /configured scoped-model order/);
  assert.match(skill, /resolves concretely to `medium`/);
  assert.match(skill, /role, model, thinking level, numeric slot, and assignment/);
  assert.match(skill, /exploration-to-execution transition/);
  assert.match(skill, /preserve the execution role map/);
  assert.match(skill, /housekeeping does not need a separate approval/);
  assert.match(skill, /Model\/role evidence ledger/);
  assert.match(skill, /Do not ask workers to self-grade/);
  assert.match(skill, /instruction adherence/);
  assert.match(skill, /small sample/);
  assert.match(skill, /recommended future roles and thinking levels/);
  assert.doesNotMatch(skill, /tmux-orchestrator/);
});
