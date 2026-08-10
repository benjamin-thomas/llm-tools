import assert from "node:assert/strict";
import test from "node:test";
import {
  assertSupportedPiVersion,
  LAST_TESTED_PI_VERSION,
  MINIMUM_PI_VERSION,
} from "../src/compatibility.js";

test("native terminal handoff accepts the tested Pi version", () => {
  assert.doesNotThrow(() => assertSupportedPiVersion(LAST_TESTED_PI_VERSION));
});

test("native terminal handoff accepts newer pre-1.0 Pi versions", () => {
  assert.doesNotThrow(() => assertSupportedPiVersion(MINIMUM_PI_VERSION));
  assert.doesNotThrow(() => assertSupportedPiVersion("0.85.0"));
  assert.doesNotThrow(() => assertSupportedPiVersion("0.99.99"));
});

test("native terminal handoff rejects versions outside its broad compatibility range", () => {
  assert.throws(() => assertSupportedPiVersion("0.82.9"), /requires Pi/);
  assert.throws(() => assertSupportedPiVersion("1.0.0"), /requires Pi/);
  assert.throws(() => assertSupportedPiVersion("not-a-version"), /Cannot parse Pi version/);
});
