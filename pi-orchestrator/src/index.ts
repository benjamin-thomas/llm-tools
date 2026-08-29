import { StringEnum } from "@earendil-works/pi-ai";
import {
  DEFAULT_MAX_BYTES,
  DEFAULT_MAX_LINES,
  formatSize,
  highlightCode,
  truncateHead,
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
  configureLiveRoom,
  deactivateOrchestrator,
  getHost,
  installWidget,
  liveRoomStatus,
  moderateLiveRoom,
  ownerIdForContext,
  postToRoom,
  prepareCoordinatorSessionSwitch,
  readLiveRoom,
  readLiveWorker,
  renameLiveWorker,
  resetWorker,
  resolveLiveHumanRequest,
  restoreOrchestrator,
  sendToWorker,
  showWorkerPicker,
  snapshot,
  spawnWorkers,
  stopWorker,
  unreadInbox,
  waitForLiveWorker,
  waitForRoomMessage,
  syncWorkerNameFromContext,
  updateActivity,
  updateModel,
  updateThinking,
} from "./host.js";
import { ORCHESTRATOR_ID, type OrchestrationMode, type SpawnRequest } from "./types.js";
import { ORCHESTRATOR_TOOL_NAME, ROOM_TOOL_NAME } from "./tool-policy.js";
import { transcriptMessages, type TranscriptEntry } from "./transcript.js";
import { shouldCloseWorker } from "./worker-exit.js";
import { findPersistedState } from "./persistence.js";

const TOOL_NAME = ORCHESTRATOR_TOOL_NAME;
const actions = ["list", "inbox", "spawn", "rename", "send", "wait", "read", "reset", "stop"] as const;
const roomActions = ["status", "configure", "post", "wait", "read", "moderate", "resolve_human"] as const;

function setSessionTools(
  pi: ExtensionAPI,
  options: { orchestrator: boolean; room: boolean },
): void {
  const active = pi.getActiveTools().filter((name) =>
    name !== TOOL_NAME && name !== ROOM_TOOL_NAME,
  );
  if (options.orchestrator) active.push(TOOL_NAME);
  if (options.room) active.push(ROOM_TOOL_NAME);
  pi.setActiveTools(active);
}

function resultText(value: unknown): { content: Array<{ type: "text"; text: string }>; details: unknown } {
  const formatted = formatToolOutput(value);
  const truncation = truncateHead(formatted, {
    maxBytes: DEFAULT_MAX_BYTES,
    maxLines: DEFAULT_MAX_LINES,
  });
  const suffix = truncation.truncated
    ? `\n\n[Output truncated to ${truncation.outputLines} lines / ${formatSize(truncation.outputBytes)}. Full structured value remains in tool-result details.]`
    : "";
  return {
    content: [{ type: "text", text: truncation.content + suffix }],
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

function installRoomAutocomplete(host: ReturnType<typeof getHost>, ctx: ExtensionContext): void {
  ctx.ui.addAutocompleteProvider((current) => ({
    triggerCharacters: ["#"],
    async getSuggestions(lines, line, col, options) {
      const beforeCursor = (lines[line] ?? "").slice(0, col);
      const match = beforeCursor.match(/(?:^|[ \t])#([^\s#]*)$/);
      if (!match) return current.getSuggestions(lines, line, col, options);
      const query = (match[1] ?? "").toLowerCase();
      const ownerId = ownerIdForContext(host, ctx);
      const names = [
        { name: "all", description: "require every live worker to respond" },
        { name: "human", description: "request human attention through the orchestrator" },
        { name: "orchestrator", description: "call the room moderator" },
        ...snapshot(host).workers
          .filter((worker) =>
            worker.id !== ownerId
            && (worker.activity === "idle" || worker.activity === "working"),
          )
          .map((worker) => ({
            name: worker.name,
            description: `${worker.activity} · ${worker.model.provider}/${worker.model.id}`,
          })),
      ];
      return {
        prefix: `#${match[1] ?? ""}`,
        items: names
          .filter(({ name }) => name.toLowerCase().startsWith(query))
          .map(({ name, description }) => ({ value: `#${name}`, label: `#${name}`, description })),
      };
    },
    applyCompletion(lines, line, col, item, prefix) {
      return current.applyCompletion(lines, line, col, item, prefix);
    },
    shouldTriggerFileCompletion(lines, line, col) {
      return current.shouldTriggerFileCompletion?.(lines, line, col) ?? true;
    },
  }));
}

export default function orchestratorExtension(pi: ExtensionAPI) {
  let expectedNameCorrection: string | undefined;
  let workerExitUnsubscribe: (() => void) | undefined;
  let closingWorker = false;

  pi.registerTool({
    name: ROOM_TOOL_NAME,
    label: "Room",
    description:
      "Participate in the active shared room. Post visible messages to named participants, explicitly request a worker reply, broadcast to all workers, contact #human, read the room, or inspect status. Named posts are tells by default; #all and expectReply calls require responses.",
    promptSnippet: "Communicate through the shared orchestrated room when room mode is active",
    promptGuidelines: [
      "Use room post with recipient names without the # prefix. Named posts are visible tells and do not wake recipients unless expectReply is true. Use all to require every other live worker to respond, and human to request human attention.",
      "Do not create peer response calls while #all is open. From the orchestrator, room wait returns at broadcast settlement or early for a moderation checkpoint; moderate continue before waiting again.",
    ],
    parameters: Type.Object({
      action: StringEnum(roomActions),
      to: Type.Optional(Type.Array(Type.String())),
      message: Type.Optional(Type.String()),
      messageId: Type.Optional(Type.String()),
      expectReply: Type.Optional(Type.Boolean()),
      replyTo: Type.Optional(Type.String()),
      after: Type.Optional(Type.Integer({ minimum: 0 })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })),
      decision: Type.Optional(StringEnum(["continue", "conclude"] as const)),
      moderationEvery: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const host = getHost();
      const ownerId = ownerIdForContext(host, ctx);
      if (!ownerId) throw new Error("The current session is not an orchestration participant.");

      switch (params.action) {
        case "status":
          return resultText(liveRoomStatus(host));
        case "configure": {
          if (params.moderationEvery === undefined) {
            throw new Error("room configure requires moderationEvery.");
          }
          configureLiveRoom(host, ctx, params.moderationEvery);
          return resultText({ configured: true, status: liveRoomStatus(host) });
        }
        case "post": {
          if (!params.message) throw new Error("room post requires message.");
          const message = postToRoom(
            host,
            ctx,
            params.to ?? [],
            params.message,
            params.replyTo,
            params.expectReply,
          );
          return resultText({ message, status: liveRoomStatus(host) });
        }
        case "wait": {
          const wait = await waitForRoomMessage(host, ctx, params.messageId, _signal);
          return resultText({
            ...(params.messageId ? { messageId: params.messageId } : {}),
            ...wait,
            status: liveRoomStatus(host),
          });
        }
        case "read": {
          const options: { after?: number; limit?: number } = {};
          if (params.after !== undefined) options.after = params.after;
          if (params.limit !== undefined) options.limit = params.limit;
          return resultText(readLiveRoom(host, ctx, options));
        }
        case "moderate": {
          if (!params.decision) throw new Error("room moderate requires decision.");
          moderateLiveRoom(host, ctx, params.decision);
          return resultText({ decision: params.decision, status: liveRoomStatus(host) });
        }
        case "resolve_human": {
          if (!params.messageId) throw new Error("room resolve_human requires messageId.");
          return resultText({
            messageId: params.messageId,
            resolved: resolveLiveHumanRequest(host, ctx, params.messageId),
            status: liveRoomStatus(host),
          });
        }
      }
    },
    renderResult(result, options, theme, context) {
      const details = result.details as Record<string, unknown> | undefined;
      const args = context.args as { action?: string };
      let display = theme.fg("dim", "Room operation complete.");
      if (args.action === "post") {
        const message = details?.message as {
          sender?: { name?: string };
          recipients?: Array<{ name?: string }>;
          broadcast?: boolean;
          text?: string;
        } | undefined;
        const targets = message?.broadcast
          ? "#all"
          : message?.recipients?.length
            ? message.recipients.map((recipient) => `#${recipient.name ?? "?"}`).join(" ")
            : "#room";
        display = `${theme.fg("accent", `${message?.sender?.name ?? "room"} → ${targets}`)}\n${message?.text ?? ""}`;
      } else if (args.action === "read") {
        const messages = Array.isArray(details?.messages)
          ? details.messages as Array<{
              sender?: { name?: string };
              recipients?: Array<{ name?: string }>;
              broadcast?: boolean;
              text?: string;
            }>
          : [];
        display = messages.length > 0
          ? messages.map((message) => {
              const targets = message.broadcast
                ? "#all"
                : message.recipients?.length
                  ? message.recipients.map((recipient) => `#${recipient.name ?? "?"}`).join(" ")
                  : "#room";
              return `${theme.fg("accent", `${message.sender?.name ?? "room"} → ${targets}`)}\n${message.text ?? ""}`;
            }).join("\n\n")
          : theme.fg("dim", "No unread room messages.");
      } else if (args.action === "configure") {
        display = theme.fg("success", "✓ Room moderation interval configured");
      } else if (args.action === "wait") {
        display = details?.settled
          ? theme.fg("success", "✓ Room response obligations settled")
          : details?.reason === "human_request"
            ? theme.fg("warning", "Room participant requested human attention")
            : theme.fg("warning", "Room moderation checkpoint required");
      } else if (args.action === "moderate") {
        display = theme.fg("success", `✓ Moderation: ${String(details?.decision ?? "complete")}`);
      } else if (args.action === "resolve_human") {
        display = theme.fg("success", "✓ Human-attention request resolved");
      }
      if (options.expanded) {
        const yaml = result.content
          .filter((block): block is { type: "text"; text: string } => block.type === "text")
          .map((block) => block.text)
          .join("\n");
        if (yaml) display += `\n\n${theme.fg("dim", "Details")}\n${highlightCode(yaml, "yaml").join("\n")}`;
      }
      return new Text(display, 0, 0);
    },
  });

  pi.registerTool({
    name: TOOL_NAME,
    label: "Orchestrator",
    description:
      "Manage live native Pi workers for the current orchestration. List, spawn, rename, send silo instructions, wait, read structured messages, reset idle workers to fresh sessions, or stop workers. Coordinator-only. In room mode use the room tool for shared deliberation.",
    promptSnippet: "Create and manage live Pi workers after the user activates /orchestrate",
    promptGuidelines: [
      "Use orchestrator to manage workers when orchestration mode is active. Choose an explicit scoped model when a task clearly benefits from it; otherwise omit models and use the configured scoped-model order. Ask the user when requested model identities are ambiguous.",
      "For silo delegation, use orchestrator send, then wait, then read. In room mode use room post, wait, read, and moderate for deliberation. Reset only idle workers when a fresh transcript is needed; it preserves worker identity, slot, name, cwd, model, and thinking level.",
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
          if (host.mode === "room") {
            throw new Error("Use room post for worker communication while room mode is active.");
          }
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
          await waitForLiveWorker(host, ctx, params.workerId, _signal);
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
    description: "Activate silo or room orchestration, open its worker picker, or stop it",
    handler: async (args, ctx) => {
      const host = getHost();
      const action = args.trim().toLowerCase();

      if (!host.active) {
        if (action && action !== "start" && action !== "silo" && action !== "room") {
          ctx.ui.notify("Usage: /orchestrate [silo|room|status|stop]", "warning");
          return;
        }
        let mode: OrchestrationMode | undefined;
        if (action === "silo") mode = "silo";
        else if (action === "room") mode = "room";
        else if (ctx.hasUI) {
          const selected = await ctx.ui.select("Orchestration communication mode", [
            "Silo — workers communicate only with the orchestrator (default)",
            "Room — workers share a moderated channel",
          ]);
          if (!selected) return;
          mode = selected.startsWith("Room") ? "room" : "silo";
        } else mode = "silo";

        try {
          activateOrchestrator(
            ctx,
            (customType, data) => pi.appendEntry(customType, data),
            mode,
          );
          setSessionTools(pi, { orchestrator: true, room: mode === "room" });
          installWidget(host, ctx);
          if (mode === "room") installRoomAutocomplete(host, ctx);
          ctx.ui.notify(
            `${mode === "room" ? "Room" : "Silo"} orchestration active with ${host.scopedModels.length} scoped models. Describe the workers you want in natural language.`,
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
          setSessionTools(pi, { orchestrator: false, room: false });
          ctx.ui.notify("Orchestration stopped.", "info");
        } catch (value) {
          ctx.ui.notify(value instanceof Error ? value.message : String(value), "error");
        }
        return;
      }
      if (action === "status") {
        const room = host.room;
        ctx.ui.notify(
          `${host.mode} · ${host.workers.length} worker${host.workers.length === 1 ? "" : "s"} · focused: ${host.focusedId}`
          + (room?.openBroadcastId ? ` · broadcast: ${room.openBroadcastId}` : "")
          + (room?.moderationRequired ? " · moderation required" : ""),
          "info",
        );
        return;
      }
      if (action) {
        ctx.ui.notify("Usage: /orchestrate [silo|room|status|stop]", "warning");
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
      let skippedInvalidState = false;
      const saved = findPersistedState(
        ctx.sessionManager.getBranch(),
        () => { skippedInvalidState = true; },
      );
      if (skippedInvalidState) {
        ctx.ui.notify(
          saved
            ? "Could not parse the latest orchestration state; using an older snapshot."
            : "Could not parse persisted orchestration state; no usable snapshot was found.",
          "warning",
        );
      }
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
    setSessionTools(pi, {
      orchestrator: coordinator,
      room: Boolean(ownerId && host.mode === "room"),
    });
    if (ownerId) installWidget(host, ctx);
    if (ownerId && host.mode === "room") installRoomAutocomplete(host, ctx);

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
