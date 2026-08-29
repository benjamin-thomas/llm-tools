export interface ActivationQueue {
  tail: Promise<void>;
}

export function createActivationQueue(): ActivationQueue {
  return { tail: Promise.resolve() };
}

export function runSerializedActivation<T>(
  queue: ActivationQueue,
  operation: () => Promise<T>,
): Promise<T> {
  const result = queue.tail.then(operation);
  queue.tail = result.then(() => undefined, () => undefined);
  return result;
}

export function requestSerializedActivation(
  queue: ActivationQueue,
  target: string,
  activate: (target: string) => Promise<void>,
): Promise<void> {
  return runSerializedActivation(queue, () => activate(target));
}
