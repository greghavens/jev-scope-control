// opencode's half of jev-scope-control. Before a tool runs, send the call and
// the context to the Python script, which asks Jev whether the call is within what the person
// asked for or agreed to. A denied call is stopped by throwing the script's
// reason, which the model sees as the tool's error.
import { spawn } from "node:child_process"
import { fileURLToPath } from "node:url"
import type { Plugin } from "@opencode-ai/plugin"

const SCRIPT = fileURLToPath(new URL("../jev-scope-control", import.meta.url))
// Above the script's 5 s Jev timeout, so the script fails open by itself.
const TIMEOUT_MS = 15_000

type Part = { type: string; text?: string; synthetic?: boolean; tool?: string; callID?: string; state?: any }
type Message = { info: { id: string; role: string }; parts: Part[] }
type Call = { tool: string; input: unknown; result: string | null; error: boolean }

function text(parts: Part[]): string {
  return parts
    .filter((p) => p.type === "text" && !p.synthetic && p.text)
    .map((p) => p.text)
    .join("\n")
}

function call(part: Part): Call {
  const state = part.state ?? {}
  let result = state.status === "completed" ? String(state.output ?? "") : state.status === "error" ? String(state.error ?? "") : null
  // The shell tool reports a failed command as completed; its exit code is only in the metadata.
  const exit = state.metadata?.exit
  const failed = typeof exit === "number" && exit !== 0
  if (failed && result !== null) result = `${result.replace(/\n+$/, "")}\nExit code: ${exit}`
  return { tool: part.tool ?? "unknown", input: state.input ?? {}, result, error: state.status === "error" || failed }
}

// Run the script with the call on stdin. Resolves its stdout, or "" on any failure: the check fails open.
function check(input: object): Promise<string> {
  return new Promise((resolve) => {
    let stdout = ""
    const child = spawn("python3", [SCRIPT], { stdio: ["pipe", "pipe", "ignore"], timeout: TIMEOUT_MS })
    child.stdout.on("data", (chunk) => (stdout += chunk))
    child.on("error", () => resolve(""))
    child.on("close", () => resolve(stdout))
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify(input))
  })
}

export const JevScopeControl: Plugin = async ({ client }) => {
  const messagesOf = async (id: string) => ((await client.session.messages({ path: { id } })).data ?? []) as Message[]

  // The deny reason when the script denies the call, else "". Never throws.
  async function review(tool: string, sessionID: string, args: unknown): Promise<string> {
    try {
      // A subagent works for the person's request too: the scope comes from the root session.
      let rootID = sessionID
      for (let depth = 0; depth < 10; depth++) {
        const parent = (await client.session.get({ path: { id: rootID } })).data?.parentID
        if (!parent) break
        rootID = parent
      }
      const root = await messagesOf(rootID)
      // The task is the person's last prompt.
      let taskIndex = -1
      for (let i = root.length - 1; i >= 0; i--) {
        const prompt = text(root[i].parts).trim()
        if (root[i].info.role === "user" && prompt) {
          taskIndex = i
          break
        }
      }
      const calls = (messages: Message[]) =>
        messages.filter((m) => m.info.role === "assistant").flatMap((m) => m.parts.filter((p) => p.type === "tool").map(call))

      // In a subagent, the calls since the task are its own.
      const stdout = await check({
        host: "opencode",
        session_id: `opencode-${rootID}`,
        ...(rootID !== sessionID ? { agent_id: sessionID } : {}),
        task: taskIndex >= 0 ? text(root[taskIndex].parts) : "",
        actions: calls(rootID === sessionID ? root.slice(taskIndex + 1) : await messagesOf(sessionID)),
        earlier_actions: calls(root.slice(0, Math.max(taskIndex, 0))),
        conversation: root.map((m) => ({ role: m.info.role, text: text(m.parts) })).filter((m) => m.text.trim()),
        tool_name: tool,
        tool_input: args,
      })
      const decision = (stdout.trim() ? JSON.parse(stdout) : {}).hookSpecificOutput
      if (decision?.permissionDecision === "deny" && typeof decision.permissionDecisionReason === "string") {
        return decision.permissionDecisionReason
      }
    } catch {
      // fail open, as the script does
    }
    return ""
  }

  return {
    "tool.execute.before": async (input, output) => {
      const reason = await review(input.tool, input.sessionID, output.args)
      if (reason) throw new Error(reason)
    },
  }
}
