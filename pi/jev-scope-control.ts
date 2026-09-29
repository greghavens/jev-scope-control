// pi's half of jev-scope-control. Before a tool runs, send the call and the
// context to the Python script, which asks
// Jev whether the call is within what the person asked for or agreed to. A denied call is blocked with the script's reason,
// which the model sees as the tool's result.
import { spawn } from "node:child_process"
import { fileURLToPath } from "node:url"
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"

const SCRIPT = fileURLToPath(new URL("../jev-scope-control", import.meta.url))
// Above the script's 5 s Jev timeout, so the script fails open by itself.
const TIMEOUT_MS = 15_000

type Call = { tool: string; input: unknown; result: string | null; error: boolean; now: boolean }

function text(content: unknown): string {
  if (typeof content === "string") return content
  if (!Array.isArray(content)) return ""
  return content
    .map((block) => (block?.type === "text" ? block.text : block?.type === "image" ? "[image]" : ""))
    .filter(Boolean)
    .join("\n")
}

// Run the script with the call on stdin. Resolves its stdout, or "" on any failure: the check fails open.
function check(input: object, signal: AbortSignal | undefined): Promise<string> {
  return new Promise((resolve) => {
    let stdout = ""
    const child = spawn("python3", [SCRIPT], { stdio: ["pipe", "pipe", "ignore"], timeout: TIMEOUT_MS, signal })
    child.stdout.on("data", (chunk) => (stdout += chunk))
    child.on("error", () => resolve(""))
    child.on("close", () => resolve(stdout))
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify(input))
  })
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event, ctx) => {
    // pi blocks the call when a handler throws, so nothing here may.
    try {
      // The branch's messages, with extension messages as "custom".
      const messages: { role: string; content?: unknown; toolCallId?: string; isError?: boolean }[] = []
      for (const entry of ctx.sessionManager.getBranch()) {
        if (entry.type === "message") messages.push(entry.message as (typeof messages)[number])
        else if (entry.type === "custom_message") messages.push({ role: "custom", content: entry.content })
      }

      // The task is the person's last message.
      let taskIndex = -1
      for (let i = messages.length - 1; i >= 0; i--) {
        const m = messages[i]
        if (m.role === "user" && text(m.content).trim()) {
          taskIndex = i
          break
        }
      }

      const calls: Call[] = []
      const byId = new Map<string, Call>()
      messages.forEach((message, i) => {
        if (message.role === "assistant" && Array.isArray(message.content)) {
          for (const block of message.content) {
            if (block?.type !== "toolCall") continue
            const call = { tool: block.name, input: block.arguments, result: null, error: false, now: i > taskIndex }
            calls.push(call)
            byId.set(block.id, call)
          }
        } else if (message.role === "toolResult") {
          const call = byId.get(message.toolCallId ?? "")
          if (call) {
            call.result = text(message.content)
            call.error = Boolean(message.isError)
          }
        }
      })
      const strip = ({ now: _, ...call }: Call) => call

      const stdout = await check(
        {
          host: "pi",
          session_id: `pi-${ctx.sessionManager.getSessionId()}`,
          task: taskIndex >= 0 ? text(messages[taskIndex].content) : "",
          actions: calls.filter((c) => c.now).map(strip),
          earlier_actions: calls.filter((c) => !c.now).map(strip),
          conversation: messages
            .filter((m) => m.role === "user" || m.role === "assistant" || m.role === "custom")
            .map((m) => ({ role: m.role === "custom" ? "hook" : m.role, text: text(m.content) }))
            .filter((m) => m.text.trim()),
          tool_name: event.toolName,
          tool_input: event.input,
        },
        ctx.signal,
      )
      const output = stdout.trim() ? JSON.parse(stdout) : {}
      const decision = output.hookSpecificOutput
      if (typeof output.systemMessage === "string" && ctx.hasUI) {
        ctx.ui.notify(output.systemMessage, decision?.permissionDecision === "deny" ? "warning" : "info")
      }
      if (decision?.permissionDecision === "deny" && typeof decision.permissionDecisionReason === "string") {
        return { block: true, reason: decision.permissionDecisionReason }
      }
    } catch {
      // fail open, as the script does
    }
  })
}
