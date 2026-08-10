import { stringify } from "yaml";

function decodeEmbeddedJson(value: unknown, depth = 0): unknown {
  if (depth > 32) return value;
  if (typeof value === "string") {
    const trimmed = value.trim();
    const looksLikeJson =
      (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
      (trimmed.startsWith("[") && trimmed.endsWith("]"));
    if (!looksLikeJson) return value;
    try {
      return decodeEmbeddedJson(JSON.parse(trimmed), depth + 1);
    } catch {
      return value;
    }
  }
  if (Array.isArray(value)) {
    return value.map((item) => decodeEmbeddedJson(item, depth + 1));
  }
  if (value && typeof value === "object") {
    const prototype = Object.getPrototypeOf(value) as unknown;
    if (prototype !== Object.prototype && prototype !== null) return value;
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, decodeEmbeddedJson(item, depth + 1)]),
    );
  }
  return value;
}

export function formatToolOutput(value: unknown): string {
  return stringify(decodeEmbeddedJson(value), { lineWidth: 0 }).trimEnd();
}
