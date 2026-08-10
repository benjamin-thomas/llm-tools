import { matchesKey } from "@earendil-works/pi-tui";

export function shouldCloseWorker(data: string, editorText: string, isWorker: boolean): boolean {
  return isWorker && editorText.length === 0 && matchesKey(data, "ctrl+d");
}
