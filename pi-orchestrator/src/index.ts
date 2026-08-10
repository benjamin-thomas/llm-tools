import { StringEnum } from "@earendil-works/pi-ai";
import {
  highlightCode,
  type ExtensionAPI,
  type ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { Text } from "@earendil-works/pi-tui";
import { formatToolOutput } from "./format.js";
import {
  activateFromContext,
  activateOrchestrator,
  assertCoordinatorContext,
  attachPersistence,
  deactivateOrchestrator,
  getHost,
  installWidget,
  ownerIdForContext,
  prepareCoordinatorSessionSwitch,
  readLiveWorker,
  renameLiveWorker,
  resetWorker,
  restoreOrchestrator,
  sendToWorker,
  showWorkerPicker,
  snapshot,
  spawnWorkers,
  stopWorker,
  unreadInbox,
  waitForLiveWorker,
  syncWorkerNameFromContext,
  updateActivity,
  updateModel,
  updateThinking,
} from "./host.js";
import { ORCHESTRATOR_ID, type SpawnRequest } from "./types.js";
import { ORCHESTRATOR_TOOL_NAME } from "./tool-policy.js";
import { transcriptMessages, type TranscriptEntry } from "./transcript.js";
import { shouldCloseWorker } from "./worker-exit.js";
import { findPersistedState } from "./persistence.js";

const TOOL_NAME = ORCHESTRATOR_TOOL_NAME;
const actions = ["list", "inbox", "spawn", "rename", "send", "wait", "read", "reset", "stop"] as const;

function setToolEnabled(pi: ExtensionAPI, enabled: boolean): void {
  const active = pi.getActiveTools().filter((name) => name !== TOOL_NAME);
  pi.setActiveTools(enabled ? [...active, TOOL_NAME] : active);
}

function resultText(value: unknown): { content: Array<{ type: "text"; text: string }>; details: unknown } {
  return {
    content: [{ type: "text", text: formatToolOutput(value) }],
    details: value,
  };
}

function workerReference(host: ReturnType<typeof getHost>, workerId: string) {
  const worker = snapshot(host).workers.find((candidate) => candidate.id === workerId);
  if (!worker) throw new Error(`Unknown worker: ${workerId}`);
  return {
    id: worker.id,
    name: worker.name,
    key: worker.key,
    model: `${worker.model.provider}/${worker.model.id}`,
  };
}

function currentWorkerId(ctx: ExtensionContext): string {
  const host = getHost();
  const ownerId = ownerIdForContext(host, ctx);
  if (!ownerId || ownerId === ORCHESTRATOR_ID) {
    throw new Error("This command must be run from a worker.");
  }
  return ownerId;
}

export default function orchestratorExtension(pi: ExtensionAPI) {
  let expectedNameCorrection: string | undefined;
  let workerExitUnsubscribe: (() => void) | undefined;
  let closingWorker = false;

  pi.registerTool({
    name: TOOL_NAME,
    label: "Orchestrator",
    description:
      "Manage live native Pi workers for the current orchestration. List, spawn, rename, send instructions, wait, read structured messages, reset idle workers to fresh sessions, or stop workers. Coordinator-only; workers cannot address peers.",
    promptSnippet: "Create and manage live Pi workers after the user activates /orchestrate",
    promptGuidelines: [
      "Use orchestrator to manage workers when orchestration mode is active. Choose an explicit scoped model when a task clearly benefits from it; otherwise omit models and use the configured scoped-model order. Ask the user when requested model identities are ambiguous.",
      "For delegated work, use orchestrator send, then wait, then read. Send returns a pre-dispatch cursor, read defaults to the worker's unread cursor, and inbox lists unread responses. Reset only idle workers when a fresh transcript is needed; it preserves worker identity, slot, name, cwd, model, and thinking level. Workers cannot message peers.",
    ],
    parameters: Type.Object({
      action: StringEnum(actions),
      count: Type.Optional(Type.Integer({ minimum: 1, maximum: 8 })),
      models: Type.Optional(Type.Array(Type.String())),
      workerId: Type.Optional(Type.String()),
      name: Type.Optional(Type.String()),
      message: Type.Optional(Type.String()),
      delivery: Type.Optional(StringEnum(["steer", "followUp"] as const)),
      cursor: Type.Optional(Type.String()),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const host = getHost();
      assertCoordinatorContext(host, ctx);

      switch (params.action) {
        case "list":
          return resultText(snapshot(host));
        case "inbox":
          return resultText({ workers: unreadInbox(host, ctx) });
        case "spawn": {
          const request: SpawnRequest = {};
          if (params.count !== undefined) request.count = params.count;
          if (params.models !== undefined) request.models = params.models;
          const workers = await spawnWorkers(host, ctx, request);
          return resultText({ created: workers, orchestration: snapshot(host) });
        }
        case "rename": {
          if (!params.workerId || !params.name) {
            throw new Error("rename requires workerId and name.");
          }
          const worker = renameLiveWorker(host, params.workerId, params.name);
          return resultText({ renamed: worker, orchestration: snapshot(host) });
        }
        case "send": {
          if (!params.workerId || !params.message) {
            throw new Error("send requires workerId and message.");
          }
          const worker = workerReference(host, params.workerId);
          const acknowledgement = await sendToWorker(
            host,
            ctx,
            params.workerId,
            params.message,
            params.delivery ?? "followUp",
          );
          return resultText({ worker, message: params.message, ...acknowledgement });
        }
        case "wait": {
          if (!params.workerId) throw new Error("wait requires workerId.");
          const worker = workerReference(host, params.workerId);
          await waitForLiveWorker(host, ctx, params.workerId);
          return resultText({ worker, settled: true });
        }
        case "read": {
          if (!params.workerId) throw new Error("read requires workerId.");
          const options: { after?: string; limit?: number } = {};
          if (params.cursor !== undefined) options.after = params.cursor;
          if (params.limit !== undefined) options.limit = params.limit;
          const worker = workerReference(host, params.workerId);
          return resultText({ worker, ...readLiveWorker(host, ctx, params.workerId, options) });
        }
        case "reset": {
          if (!params.workerId) throw new Error("reset requires workerId.");
          const worker = await resetWorker(host, ctx, params.workerId);
          return resultText({ reset: worker });
        }
        case "stop": {
          if (!params.workerId) throw new Error("stop requires workerId.");
          const worker = workerReference(host, params.workerId);
          await stopWorker(host, params.workerId);
          return resultText({ stopped: worker, orchestration: snapshot(host) });
        }
      }
    },
    renderResult(result, options, theme, context) {
      const yaml = result.content
        .filter((block): block is { type: "text"; text: string } => block.type === "text")
        .map((block) => block.text)
        .join("\n");
      const args = context.args as { action?: string; message?: string };
      const details = result.details as Record<string, unknown> | undefined;
      const worker = details?.worker as { name?: string } | undefined;
      let display: string;

      if (args.action === "send" && worker?.name) {
        display = `${theme.fg("accent", `orchestrator → ${worker.name}`)}\n${String(details?.message ?? args.message ?? "")}`;
      } else if (args.action === "read" && worker?.name) {
        const entries = Array.isArray(details?.messages)
          ? details.messages as TranscriptEntry[]
          : [];
        const messages = transcriptMessages(worker.name, entries);
        display = messages.length > 0
          ? messages.map(({ sender, receiver, text }) =>
              `${theme.fg("accent", `${sender} → ${receiver}`)}\n${text}`,
            ).join("\n\n")
          : theme.fg("dim", "No new prompt messages.");
      } else if (args.action === "wait" && worker?.name) {
        display = theme.fg("success", `✓ ${worker.name} is idle`);
      } else if (args.action === "reset") {
        const reset = details?.reset as { name?: string } | undefined;
        display = theme.fg("success", `✓ Reset ${reset?.name ?? "worker"} with a fresh session`);
      } else if (args.action === "stop") {
        const stopped = details?.stopped as { name?: string } | undefined;
        display = theme.fg("success", `✓ Stopped ${stopped?.name ?? "worker"}`);
      } else if (args.action === "rename") {
        const renamed = details?.renamed as { name?: string } | undefined;
        display = theme.fg("success", `✓ Worker renamed to ${renamed?.name ?? "worker"}`);
      } else if (args.action === "spawn") {
        const created = Array.isArray(details?.created)
          ? details.created as Array<{ name?: string; model?: { provider?: string; id?: string } }>
          : [];
        display = created.length > 0
          ? created.map((item) =>
              theme.fg("success", `✓ Started ${item.name ?? "worker"}`) +
              theme.fg("dim", ` (${item.model?.provider ?? "?"}/${item.model?.id ?? "?"})`),
            ).join("\n")
          : theme.fg("dim", "No workers started.");
      } else if (args.action === "list" || args.action === "inbox") {
        const workers = Array.isArray(details?.workers)
          ? details.workers as Array<{ key?: string; name?: string; activity?: string; unread?: number; unreadCount?: number }>
          : [];
        display = workers.length > 0
          ? workers.map((item) => {
              const unread = item.unread ?? item.unreadCount ?? 0;
              return `${item.key ?? ""} ${item.name ?? "worker"} — ${item.activity ?? "unknown"}${unread > 0 ? ` +${unread}` : ""}`;
            }).join("\n")
          : theme.fg("dim", args.action === "inbox" ? "No unread worker responses." : "No live workers.");
      } else {
        display = theme.fg("dim", "Orchestrator operation complete.");
      }

      if (options.expanded && yaml) {
        display += `\n\n${theme.fg("dim", "Details")}\n${highlightCode(yaml, "yaml").join("\n")}`;
      }
      return new Text(display, 0, 0);
    },
  });

  pi.registerCommand("orchestrate", {
    description: "Activate orchestration mode, open its worker picker, or stop it",
    handler: async (args, ctx) => {
      const host = getHost();
      const action = args.trim().toLowerCase();

      if (!host.active) {
        if (action && action !== "start") {
          ctx.ui.notify("Run /orchestrate before using orchestration subcommands.", "warning");
          return;
        }
        try {
          activateOrchestrator(ctx, (customType, data) => pi.appendEntry(customType, data));
          setToolEnabled(pi, true);
          installWidget(host, ctx);
          ctx.ui.notify(
            `Orchestration active with ${host.scopedModels.length} scoped models. Describe the workers you want in natural language.`,
            "info",
          );
        } catch (value) {
          ctx.ui.notify(value instanceof Error ? value.message : String(value), "error");
        }
        return;
      }

      if (action === "stop") {
        try {
          await deactivateOrchestrator(
            host,
            ctx,
            (customType, data) => pi.appendEntry(customType, data),
          );
          setToolEnabled(pi, false);
          ctx.ui.notify("Orchestration stopped.", "info");
        } catch (value) {
          ctx.ui.notify(value instanceof Error ? value.message : String(value), "error");
        }
        return;
      }
      if (action === "status") {
        ctx.ui.notify(
          `${host.workers.length} worker${host.workers.length === 1 ? "" : "s"}; focused: ${host.focusedId}`,
          "info",
        );
        return;
      }
      if (action && action !== "start") {
        ctx.ui.notify("Usage: /orchestrate [start|status|stop]", "warning");
        return;
      }
      await showWorkerPicker(host, ctx);
    },
  });

  pi.registerCommand("worker-name", {
    description: "Rename the currently focused worker",
    handler: async (args, ctx) => {
      const name = args.trim();
      if (!name) {
        ctx.ui.notify("Usage: /worker-name <name>", "warning");
        return;
      }
      try {
        const worker = renameLiveWorker(getHost(), currentWorkerId(ctx), name);
        ctx.ui.notify(`Worker renamed to ${worker.name}.`, "info");
      } catch (value) {
        ctx.ui.notify(value instanceof Error ? value.message : String(value), "error");
      }
    },
  });

  pi.registerShortcut("alt+s", {
    description: "Open orchestrator worker picker",
    handler: async (ctx) => {
      const host = getHost();
      if (!host.active) {
        ctx.ui.notify("Run /orchestrate first.", "warning");
        return;
      }
      await showWorkerPicker(host, ctx);
    },
  });


  pi.on("session_start", async (_event, ctx) => {
    workerExitUnsubscribe?.();
    workerExitUnsubscribe = undefined;
    closingWorker = false;

    let host = getHost();
    if (!host.active) {
      const saved = findPersistedState(ctx.sessionManager.getBranch());
      if (saved?.active) {
        try {
          host = await restoreOrchestrator(
            ctx,
            saved,
            (customType, data) => pi.appendEntry(customType, data),
          );
          ctx.ui.notify(
            `Restored orchestration with ${host.workers.length} worker${host.workers.length === 1 ? "" : "s"}.`,
            "info",
          );
        } catch (value) {
          ctx.ui.notify(
            `Could not restore orchestration: ${value instanceof Error ? value.message : String(value)}`,
            "error",
          );
        }
      }
    }

    const ownerId = host.active ? ownerIdForContext(host, ctx) : null;
    const coordinator = ownerId === ORCHESTRATOR_ID;
    if (coordinator) {
      attachPersistence(host, (customType, data) => pi.appendEntry(customType, data));
    }
    setToolEnabled(pi, coordinator);
    if (ownerId) installWidget(host, ctx);

    if (ctx.mode === "tui" && ownerId && ownerId !== ORCHESTRATOR_ID) {
      const workerId = ownerId;
      workerExitUnsubscribe = ctx.ui.onTerminalInput((data) => {
        if (!shouldCloseWorker(data, ctx.ui.getEditorText(), true)) return undefined;
        if (!closingWorker) {
          closingWorker = true;
          void stopWorker(host, workerId).catch((value: unknown) => {
            closingWorker = false;
            ctx.ui.notify(value instanceof Error ? value.message : String(value), "error");
          });
        }
        return { consume: true };
      });
    }
  });

  pi.on("session_before_switch", async (_event, ctx) => {
    await prepareCoordinatorSessionSwitch(getHost(), ctx);
  });

  pi.on("agent_start", (_event, ctx) => updateActivity(getHost(), ctx, "working"));
  pi.on("agent_settled", (_event, ctx) => updateActivity(getHost(), ctx, "idle"));
  pi.on("model_select", (event, ctx) => updateModel(getHost(), ctx, event.model));
  pi.on("thinking_level_select", (event, ctx) => updateThinking(getHost(), ctx, event.level));
  pi.on("session_info_changed", (event, ctx) => {
    if (event.name === expectedNameCorrection) {
      expectedNameCorrection = undefined;
      return;
    }
    try {
      syncWorkerNameFromContext(getHost(), ctx, event.name);
    } catch (value) {
      const host = getHost();
      const ownerId = ownerIdForContext(host, ctx);
      const priorName = host.workers.find(({ record }) => record.id === ownerId)?.record.name;
      if (priorName) {
        expectedNameCorrection = priorName;
        pi.setSessionName(priorName);
      }
      ctx.ui.notify(value instanceof Error ? value.message : String(value), "error");
    }
  });
  pi.on("session_shutdown", (_event, ctx) => {
    getHost().persist?.();
    workerExitUnsubscribe?.();
    workerExitUnsubscribe = undefined;
    ctx.ui.setWidget("pi-orchestrator", undefined);
  });
}
