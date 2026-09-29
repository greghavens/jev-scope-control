// pi's half of jev-scope-control. Before a tool runs, send the call, the
// person's last message, the conversation and the calls since that message to
// the Python script, which asks Jev whether the call is within what the person
// asked for or agreed to. A denied call is blocked with the script's reason,
// which the model sees as the tool's result.
import { spawn } from "node:child_process"
import { fileURLToPath } from "node:url"
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"

const SCRIPT = fileURLToPath(new URL("../jev-scope-control", import.meta.url))
// Above the script's 5 s Jev timeout, so the script fails open by itself.
const TIMEOUT_MS = 15_000

type Call = { tool: string; input: unknown; result: string | null }

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
      type Message = { role: string; content?: unknown; toolCallId?: string; customType?: string }
      const messages: Message[] = []
      for (const entry of ctx.sessionManager.getBranch()) {
        if (entry.type === "message") messages.push(entry.message as Message)
        else if (entry.type === "compaction") messages.push({ role: "summary", content: entry.summary })
      }

      // The request is the person's last message; the calls since it are what the model has done for it.
      let taskIndex = -1
      for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].role === "user" && text(messages[i].content).trim()) {
          taskIndex = i
          break
        }
      }
      if (taskIndex < 0) return

      const calls: Call[] = []
      const byId = new Map<string, Call>()
      messages.slice(taskIndex + 1).forEach((message) => {
        if (message.role === "assistant" && Array.isArray(message.content)) {
          for (const block of message.content) {
            if (block?.type !== "toolCall" || block.id === event.toolCallId) continue
            const call = { tool: block.name, input: block.arguments, result: null }
            calls.push(call)
            byId.set(block.id, call)
          }
        } else if (message.role === "toolResult") {
          const call = byId.get(message.toolCallId ?? "")
          if (call) call.result = text(message.content)
        }
      })

      const stdout = await check(
        {
          host: "pi",
          session_id: `pi-${ctx.sessionManager.getSessionId()}`,
          cwd: ctx.cwd,
          tool_name: event.toolName,
          tool_input: event.input,
          task: text(messages[taskIndex].content),
          conversation: messages
            .slice(0, taskIndex)
            .filter((m) => m.role === "user" || m.role === "assistant" || m.role === "summary")
            .map((m) => ({ role: m.role, text: text(m.content) })),
          actions: calls,
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
