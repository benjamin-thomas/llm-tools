import { fileURLToPath } from "node:url";
import {
  type AgentSessionRuntime,
  type CreateAgentSessionRuntimeFactory,
  createAgentSessionFromServices,
  createAgentSessionRuntime,
  createAgentSessionServices,
  getAgentDir,
  InteractiveMode,
  SessionManager,
} from "@earendil-works/pi-coding-agent";
import type { Terminal } from "@earendil-works/pi-tui";
import type { ScopedModelSpec, WorkerRecord } from "./types.js";
import { modelKey } from "./registry.js";
import { workerToolNames } from "./tool-policy.js";

interface PrivateTui {
  start(): void;
  stop(): void;
  requestRender(force?: boolean): void;
  terminal: Terminal;
}

export interface InteractiveModeAccess {
  readonly ui: PrivateTui;
  run(): Promise<void>;
  stop(): void;
}

export interface WorkerHandle {
  runtime: AgentSessionRuntime;
  mode: InteractiveMode;
  started: boolean;
  state: "suspended" | "active" | "stopped";
  start(): void;
  suspend(): void;
  resume(): void;
  dispose(): Promise<void>;
}

export interface WorkerRuntimeCallbacks {
  isFocused(): boolean;
  onState(state: "active" | "suspended" | "stopped"): void;
  onError(error: Error): void;
}

function resetExtendedKeyboardModes(): void {
  process.stdout.write("\x1b[<999u\x1b[>4;0m");
}

export function getInteractiveModeAccess(mode: unknown): InteractiveModeAccess {
  // Pi has no public suspend/resume API. Keep private access and its runtime
  // capability check isolated here so a future Pi change fails with context.
  const access = mode as Partial<InteractiveModeAccess>;
  const ui = access.ui as Partial<PrivateTui> | undefined;
  if (
    typeof access.run !== "function"
    || typeof access.stop !== "function"
    || !ui
    || typeof ui.start !== "function"
    || typeof ui.stop !== "function"
    || typeof ui.requestRender !== "function"
    || typeof ui.terminal !== "object"
    || ui.terminal === null
  ) {
    throw new Error(
      "Pi InteractiveMode private TUI is incompatible with pi-orchestrator terminal handoff.",
    );
  }
  return access as InteractiveModeAccess;
}

function mutedTerminal(terminal: Terminal): Terminal {
  return new Proxy(terminal, {
    get(target, property) {
      const value = Reflect.get(target, property, target);
      return typeof value === "function" ? () => undefined : value;
    },
  });
}

export function stopWithoutTerminalEffects(access: InteractiveModeAccess): void {
  const terminal = access.ui.terminal;
  access.ui.terminal = mutedTerminal(terminal);
  try {
    access.stop();
  } finally {
    // Pi 0.84 can replace its renderer while stopping fullscreen mode. `ui` is
    // a stable proxy, so restoring here updates whichever renderer is current.
    access.ui.terminal = terminal;
  }
}

function installTerminalGate(access: InteractiveModeAccess, callbacks: WorkerRuntimeCallbacks): void {
  const terminal = access.ui.terminal;
  const setProgress = terminal.setProgress.bind(terminal);
  const setTitle = terminal.setTitle.bind(terminal);
  terminal.setProgress = (active) => {
    if (callbacks.isFocused()) setProgress(active);
  };
  terminal.setTitle = (title) => {
    if (callbacks.isFocused()) setTitle(title);
  };
}

export async function createWorkerHandle(
  worker: WorkerRecord,
  getScope: () => readonly ScopedModelSpec[],
  callbacks: WorkerRuntimeCallbacks,
  options: { extensionEntry?: string; resumeExistingSession?: boolean } = {},
): Promise<WorkerHandle> {
  const extensionEntry = options.extensionEntry
    ?? fileURLToPath(new URL("./index.ts", import.meta.url));
  const resumeExistingSession = options.resumeExistingSession ?? false;
  const sessionManager = resumeExistingSession && worker.sessionFile
    ? SessionManager.open(worker.sessionFile, undefined, worker.cwd)
    : SessionManager.create(worker.cwd);
  if (resumeExistingSession) {
    const resumedContext = sessionManager.buildSessionContext();
    if (resumedContext.model) {
      worker.model = {
        provider: resumedContext.model.provider,
        id: resumedContext.model.modelId,
      };
    }
    if (isThinkingLevel(resumedContext.thinkingLevel)) {
      worker.thinkingLevel = resumedContext.thinkingLevel;
    }
  }

  const createRuntime: CreateAgentSessionRuntimeFactory = async ({
    cwd,
    sessionManager,
    sessionStartEvent,
  }) => {
    const services = await createAgentSessionServices({
      cwd,
      agentDir: getAgentDir(),
      resourceLoaderOptions: { additionalExtensionPaths: [extensionEntry] },
    });
    const available = await services.modelRuntime.getAvailable();
    const availableByKey = new Map(available.map((model) => [modelKey(model), model]));
    const scopedModels = getScope().map((spec) => {
      const model = availableByKey.get(modelKey(spec));
      if (!model) throw new Error(`Scoped model is unavailable in worker runtime: ${modelKey(spec)}`);
      return spec.thinkingLevel === undefined
        ? { model }
        : { model, thinkingLevel: spec.thinkingLevel };
    });
    const workerModelKey = modelKey(worker.model);
    if (!getScope().some((spec) => modelKey(spec) === workerModelKey)) {
      throw new Error(`Worker model is outside the restored scoped models: ${workerModelKey}`);
    }
    const model = availableByKey.get(workerModelKey);
    if (!model) throw new Error(`Worker model is unavailable: ${workerModelKey}`);

    return {
      ...(await createAgentSessionFromServices({
        services,
        sessionManager,
        ...(sessionStartEvent ? { sessionStartEvent } : {}),
        model,
        thinkingLevel: worker.thinkingLevel,
        scopedModels,
      })),
      services,
      diagnostics: services.diagnostics,
    };
  };

  worker.sessionId = sessionManager.getSessionId();
  const sessionFile = sessionManager.getSessionFile();
  if (sessionFile !== undefined) worker.sessionFile = sessionFile;
  const runtime = await createAgentSessionRuntime(createRuntime, {
    cwd: worker.cwd,
    agentDir: getAgentDir(),
    sessionManager,
    sessionStartEvent: {
      type: "session_start",
      reason: resumeExistingSession ? "resume" : "startup",
    },
  });
  runtime.session.setSessionName(worker.name);
  runtime.session.setActiveToolsByName(workerToolNames(runtime.session.getActiveToolNames()));

  const mode = new InteractiveMode(runtime, {
    migratedProviders: [],
    ...(runtime.modelFallbackMessage ? { modelFallbackMessage: runtime.modelFallbackMessage } : {}),
    initialImages: [],
    initialMessages: [],
  });
  const access = getInteractiveModeAccess(mode);
  installTerminalGate(access, callbacks);

  const handle: WorkerHandle = {
    runtime,
    mode,
    started: false,
    state: "suspended",
    start() {
      if (handle.state === "stopped") return;
      if (handle.started) {
        handle.resume();
        return;
      }
      handle.started = true;
      handle.state = "active";
      callbacks.onState("active");
      void access.run().catch((value: unknown) => {
        callbacks.onError(value instanceof Error ? value : new Error(String(value)));
      });
    },
    suspend() {
      if (handle.state === "stopped") return;
      access.ui.stop();
      resetExtendedKeyboardModes();
      handle.state = "suspended";
      callbacks.onState("suspended");
    },
    resume() {
      if (handle.state === "stopped") return;
      access.ui.start();
      access.ui.requestRender(true);
      handle.state = "active";
      callbacks.onState("active");
    },
    async dispose() {
      if (handle.state === "stopped") return;
      if (callbacks.isFocused()) access.stop();
      else stopWithoutTerminalEffects(access);
      await runtime.dispose();
      handle.state = "stopped";
      callbacks.onState("stopped");
    },
  };
  return handle;
}

function isThinkingLevel(value: string): value is WorkerRecord["thinkingLevel"] {
  return value === "off"
    || value === "minimal"
    || value === "low"
    || value === "medium"
    || value === "high"
    || value === "xhigh"
    || value === "max";
}
