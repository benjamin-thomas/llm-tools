import { VERSION } from "@earendil-works/pi-coding-agent";

export const MINIMUM_PI_VERSION = "0.83.0";
export const LAST_TESTED_PI_VERSION = "0.84.1";
const NEXT_UNSUPPORTED_PI_VERSION = "1.0.0";

type ParsedVersion = readonly [major: number, minor: number, patch: number];

function parseVersion(version: string): ParsedVersion {
  const match = /^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/.exec(version);
  if (!match) throw new Error(`Cannot parse Pi version: ${version}`);
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function compareVersions(left: ParsedVersion, right: ParsedVersion): number {
  for (let index = 0; index < left.length; index += 1) {
    const difference = left[index]! - right[index]!;
    if (difference !== 0) return difference;
  }
  return 0;
}

export function assertSupportedPiVersion(version = VERSION): void {
  const current = parseVersion(version);
  const minimum = parseVersion(MINIMUM_PI_VERSION);
  const nextUnsupported = parseVersion(NEXT_UNSUPPORTED_PI_VERSION);
  if (compareVersions(current, minimum) < 0 || compareVersions(current, nextUnsupported) >= 0) {
    throw new Error(
      `pi-orchestrator requires Pi >=${MINIMUM_PI_VERSION} and <${NEXT_UNSUPPORTED_PI_VERSION}; ` +
      `current Pi is ${version}.`,
    );
  }
}
