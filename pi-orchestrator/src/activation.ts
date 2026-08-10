export interface ActivationQueue {
  inProgress: Promise<void> | null;
  queuedTarget: string | null;
}

export async function requestSerializedActivation(
  queue: ActivationQueue,
  target: string,
  activate: (target: string) => Promise<void>,
): Promise<void> {
  queue.queuedTarget = target;
  if (queue.inProgress) {
    await queue.inProgress;
    return;
  }

  while (queue.queuedTarget !== null) {
    const next = queue.queuedTarget;
    queue.queuedTarget = null;
    const operation = activate(next);
    queue.inProgress = operation;
    try {
      await operation;
    } finally {
      queue.inProgress = null;
    }
  }
}
