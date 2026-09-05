import type {
  ChatModelAdapter,
  ThreadAssistantMessagePart,
  ThreadMessageLike,
} from "@assistant-ui/react";

const API_BASE = "http://localhost:8000";
const SESSION_STORAGE_KEY = "demo-agent-session-id";

type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export type DiffLine = {
  kind: "context" | "added" | "removed";
  text: string;
};

export type CodeDiffPreview = {
  type: "code_diff";
  path: string;
  additions: number;
  deletions: number;
  lines: DiffLine[];
};

type PendingCall = {
  id: string;
  name: string;
  args: { [key: string]: JsonValue };
  permission: {
    action: "allow" | "ask" | "deny";
    requests: { permission: string; target: string }[];
    reason: string | null;
  };
  decision: "approved" | "rejected" | null;
  outcome: "completed" | "rejected" | "denied" | "failed" | null;
  output: string | null;
  preview: CodeDiffPreview | null;
};

type PendingApproval = {
  remaining_turns: number;
  outputs_committed: boolean;
  calls: PendingCall[];
};

type SessionSnapshot = {
  id: number;
  summary: string;
  pending_approval: PendingApproval | null;
};

type AgentEvent =
  | { type: "text_delta"; text: string }
  | {
      type: "tool_call";
      id: string;
      name: string;
      args: { [key: string]: JsonValue };
      approval_required?: boolean;
      preview?: CodeDiffPreview;
    }
  | {
      type: "tool_result";
      id: string;
      content: string;
      outcome?: "completed" | "rejected" | "denied" | "failed";
    }
  | { type: "approval_required"; call_ids: string[] }
  | { type: "done"; content: string }
  | { type: "max_turns" }
  | { type: "error"; message: string };

let sessionPromise: Promise<SessionSnapshot> | null = null;
let resumePendingOnLoad = false;

async function responseError(response: Response): Promise<Error> {
  const data = (await response.json().catch(() => null)) as { detail?: string } | null;
  return new Error(data?.detail ?? `请求失败：${response.status}`);
}

async function createSession(): Promise<SessionSnapshot> {
  const response = await fetch(`${API_BASE}/api/sessions`, { method: "POST" });
  if (!response.ok) throw await responseError(response);
  const { id } = (await response.json()) as { id: number };
  localStorage.setItem(SESSION_STORAGE_KEY, String(id));
  return { id, summary: "(空会话)", pending_approval: null };
}

async function loadSession(): Promise<SessionSnapshot> {
  const stored = Number(localStorage.getItem(SESSION_STORAGE_KEY));
  if (!Number.isInteger(stored) || stored <= 0) return createSession();

  const response = await fetch(`${API_BASE}/api/sessions/${stored}`);
  if (response.status === 404) {
    localStorage.removeItem(SESSION_STORAGE_KEY);
    return createSession();
  }
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<SessionSnapshot>;
}

const getSession = (): Promise<SessionSnapshot> => {
  sessionPromise ??= loadSession();
  return sessionPromise;
};

function isReadyToResume(batch: PendingApproval | null): boolean {
  return (
    batch !== null &&
    !batch.calls.some(
      call =>
        call.permission.action === "ask" &&
        call.decision === null &&
        call.outcome === null,
    )
  );
}

export async function submitToolDecision(
  callId: string,
  approved: boolean,
): Promise<{ output: string | null }> {
  const { id } = await getSession();
  const action = approved ? "approve" : "reject";
  const response = await fetch(
    `${API_BASE}/api/sessions/${id}/approvals/${encodeURIComponent(callId)}/${action}`,
    { method: "POST" },
  );
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<{ output: string | null }>;
}

function restoredToolPart(call: PendingCall): ThreadAssistantMessagePart {
  return {
    type: "tool-call",
    toolCallId: call.id,
    toolName: call.name,
    args: call.args,
    argsText: JSON.stringify(call.args, null, 2),
    ...(call.output !== null && { result: call.output }),
    ...(["rejected", "denied", "failed"].includes(call.outcome ?? "") && {
      isError: true,
    }),
    ...(call.preview !== null && { artifact: call.preview }),
    ...(call.permission.action === "ask" && call.outcome === null && {
      approval: {
        id: call.id,
        ...(call.decision !== null && {
          approved: call.decision === "approved",
        }),
      },
    }),
  };
}

export type InitialSessionState = {
  messages: ThreadMessageLike[];
  resumeOnLoad: boolean;
};

export async function loadPendingMessages(): Promise<InitialSessionState> {
  const session = await getSession();
  const batch = session.pending_approval;
  if (!batch) return { messages: [], resumeOnLoad: false };

  const hasPendingDecision = !isReadyToResume(batch);
  resumePendingOnLoad = !hasPendingDecision;
  return {
    messages: [
      {
        role: "assistant",
        content: [
          {
            type: "text",
            text: "页面刷新前，Agent 请求执行以下工具：",
          },
          ...batch.calls.map(restoredToolPart),
        ],
        status: hasPendingDecision
          ? { type: "requires-action", reason: "tool-calls" }
          : { type: "complete", reason: "unknown" },
      },
    ],
    resumeOnLoad: !hasPendingDecision,
  };
}

/** 逐行解析 SSE：chunk 可能含多条或半条消息，按空行分帧。 */
async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<AgentEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let separator;
      while ((separator = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        if (frame.startsWith("data: ")) {
          yield JSON.parse(frame.slice(6)) as AgentEvent;
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export const chatAdapter: ChatModelAdapter = {
  async *run({ messages, abortSignal, unstable_getMessage }) {
    const last = messages.at(-1);
    const activeMessage = unstable_getMessage();
    const approvalParts =
      activeMessage.role === "assistant"
        ? (activeMessage.content.filter(
            part => part.type !== "data",
          ) as ThreadAssistantMessagePart[])
        : [];
    const session = await getSession();
    const isResume =
      resumePendingOnLoad ||
      isReadyToResume(session.pending_approval) ||
      approvalParts.some(
        part => part.type === "tool-call" && part.approval?.approved !== undefined,
      );

    let response: Response;
    try {
      if (isResume) {
        // 每次按钮点击都已先持久化决定；这里不能重放消息中更早批次的审批。
        resumePendingOnLoad = false;
        response = await fetch(`${API_BASE}/api/sessions/${session.id}/resume`, {
          method: "POST",
          signal: abortSignal,
        });
      } else {
        const text =
          last?.content.find(part => part.type === "text")?.text ?? "";
        response = await fetch(`${API_BASE}/api/sessions/${session.id}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text }),
          signal: abortSignal,
        });
      }
    } catch (error) {
      sessionPromise = null;
      throw error;
    }

    if (!response.ok || !response.body) {
      sessionPromise = null;
      throw await responseError(response);
    }

    // assistant-ui 约定每次 yield 的是当前累积的完整 content，而不是增量。
    const parts: ThreadAssistantMessagePart[] = isResume
      ? [...approvalParts]
      : [];
    let currentText = "";
    let currentTextIndex: number | null = null;
    let streamError: Error | null = null;
    let terminated = false;

    try {
      for await (const event of readSse(response.body)) {
        if (event.type === "text_delta") {
          if (currentTextIndex === null) {
            currentText = "";
            currentTextIndex = parts.length;
            parts.push({ type: "text", text: "" });
          }
          currentText += event.text;
          parts[currentTextIndex] = { type: "text", text: currentText };
        } else if (event.type === "tool_call") {
          currentText = "";
          currentTextIndex = null;
          parts.push({
            type: "tool-call",
            toolCallId: event.id,
            toolName: event.name,
            args: event.args,
            argsText: JSON.stringify(event.args, null, 2),
            ...(event.preview && { artifact: event.preview }),
            ...(event.approval_required && {
              approval: { id: event.id },
            }),
          });
        } else if (event.type === "tool_result") {
          const index = parts.findIndex(
            part => part.type === "tool-call" && part.toolCallId === event.id,
          );
          const toolPart = parts[index];
          if (toolPart?.type === "tool-call") {
            parts[index] = {
              ...toolPart,
              result: event.content,
              ...(["rejected", "denied", "failed"].includes(
                event.outcome ?? "",
              ) && {
                isError: true,
              }),
            };
          }
        } else if (event.type === "approval_required") {
          terminated = true;
          resumePendingOnLoad = false;
          sessionPromise = null;
          yield {
            content: [...parts],
            status: { type: "requires-action", reason: "tool-calls" },
          };
          return;
        } else if (event.type === "max_turns") {
          terminated = true;
          resumePendingOnLoad = false;
          sessionPromise = null;
          parts.push({ type: "text", text: "[达到最大轮次，停止]" });
        } else if (event.type === "error") {
          terminated = true;
          const errorText = `[请求失败] ${event.message}`;
          if (currentTextIndex === null) {
            parts.push({ type: "text", text: errorText });
          } else {
            currentText = currentText
              ? `${currentText}\n\n${errorText}`
              : errorText;
            parts[currentTextIndex] = { type: "text", text: currentText };
          }
          streamError = new Error(event.message);
        } else if (event.type === "done") {
          terminated = true;
          resumePendingOnLoad = false;
          sessionPromise = null;
        }

        yield { content: [...parts] };
        if (streamError) throw streamError;
      }

      if (!terminated) {
        const message = "SSE 在任务完成前中断";
        parts.push({ type: "text", text: `[请求失败] ${message}` });
        yield { content: [...parts] };
        streamError = new Error(message);
        throw streamError;
      }
    } catch (error) {
      sessionPromise = null;
      const aborted = error instanceof DOMException && error.name === "AbortError";
      if (!streamError && !aborted) {
        const message = error instanceof Error ? error.message : "流式请求中断";
        parts.push({ type: "text", text: `[请求失败] ${message}` });
        yield { content: [...parts] };
      }
      throw error;
    }
  },
};
