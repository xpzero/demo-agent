import { useEffect } from "react";
import { getSessionHistory, resumeSession } from "@/adapter";
import type { SessionHistory } from "@/adapter";
import type { ChatMessage } from "@/chat/message";
import { historyToMessages } from "@/chat/messageHistory";
import { applyChatEvent, type TurnIds } from "@/chat/messageUpdates";
import { useSessionStore } from "@/stores/session";

/** 跑在生成器结尾的终态事件类型（terminal 判定，缺口 3）。 */
const TERMINAL_TYPES = new Set(["done", "error", "stopped", "max_turns"]);

/** 历史里是否存在未完成助手消息（与后端 409 同源判据）。 */
export function hasUnfinishedAssistant(messages: ChatMessage[]): boolean {
  const last = messages[messages.length - 1];
  return last !== undefined && last.kind === "assistant" && last.finishKind === "unfinished";
}

interface ResumeDeps {
  /** 发送流程的运行标志：恢复进行中阻止重复发送 */
  runningRef: { current: boolean };
  /** 状态回调：running/resuming/error 由 useChat 持有 */
  onBegin: () => void;
  onEnd: (error?: string) => void;
  setMessages: (updater: (prev: ChatMessage[]) => ChatMessage[]) => void;
  setSummaryCursor: (cursor: number | null) => void;
}

/**
 * 恢复流程：历史就绪且末条为用户消息/未完成助手消息时，连 resume 接管直播。
 * 三结局：直播恢复（复用 applyChatEvent）/ 204 解锁并重拉历史 / 失败提示。
 */
export function useSessionResume(
  historyReady: boolean,
  messages: ChatMessage[],
  { runningRef, onBegin, onEnd, setMessages, setSummaryCursor }: ResumeDeps,
) {
  useEffect(() => {
    if (!historyReady || useSessionStore.getState().isNewSession) {
      return;
    }
    if (runningRef.current) {
      return;
    }
    const last = messages[messages.length - 1];
    const shouldResume =
      last !== undefined &&
      (last.kind === "user" || hasUnfinishedAssistant(messages));
    if (!shouldResume) {
      return;
    }

    const { sessionId } = useSessionStore.getState();
    let cancelled = false;
    onBegin();
    void (async () => {
      try {
        const stream = await resumeSession(sessionId);
        if (cancelled) {
          return;
        }
        if (stream === null) {
          // 204：后端已无进行中消息——重拉历史对齐（可能刚判死收口）
          const history = await getSessionHistory(sessionId);
          if (!cancelled && history) {
            setSummaryCursor(history.session.summary_upto_message_id);
            setMessages(() => historyToMessages(history.messages));
            useSessionStore.getState().setCurrentMessageId(history.session.current_message_id);
          }
          return;
        }
        const ids: TurnIds =
          last.kind === "assistant"
            ? { userId: `resume-${last.id}`, assistantId: last.id }
            : { userId: last.id, assistantId: `resume-${last.id}` };
        let sawTerminal = false;
        for await (const event of stream) {
          if (cancelled) {
            break;
          }
          if (event.type === "resume_snapshot") {
            const history = event.history as SessionHistory;
            setSummaryCursor(history.session.summary_upto_message_id);
            setMessages(() => historyToMessages(history.messages));
            useSessionStore.getState().setCurrentMessageId(history.session.current_message_id);
            continue;
          }
          if (event.type === "user_message") {
            continue; // 恢复流不重复用户消息（历史已含）
          }
          setMessages((prev) => applyChatEvent(prev, event, ids, Date.now() / 1000));
          if ("message_id" in event && event.message_id !== undefined) {
            useSessionStore.getState().setCurrentMessageId(event.message_id);
          }
          if (TERMINAL_TYPES.has(event.type)) {
            sawTerminal = true;
          }
        }
        if (!sawTerminal && !cancelled) {
          // 断连且未见 terminal → 全量重连由用户刷新触发（第一版已知限制）
          onEnd("连接中断，请刷新重试");
          return;
        }
        onEnd();
      } catch (cause) {
        if (!cancelled) {
          onEnd(cause instanceof Error ? cause.message : String(cause));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [historyReady, messages.length === 0 ? "empty" : "nonempty", useSessionStore.getState().sessionGeneration]);
}
