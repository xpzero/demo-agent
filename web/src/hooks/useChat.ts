import { useEffect, useRef, useState } from "react";
import { getSessionHistory, streamChat } from "@/adapter";
import { useSessionStore } from "@/stores/session";
import { historyToEntries, syncEntryTimestamps, type ChatEntry } from "./chatHistory";

export type { ChatEntry } from "./chatHistory";

/**
 * 页内聊天历史：entries 数组顺序即时间序。
 * runningRef 守卫保证同一时刻最多一条进行中的流，流式事件恒定追加进最后一条 assistant 条目。
 */
export function useChat(onConversationChanged: () => void) {
  const sessionId = useSessionStore((state) => state.sessionId);
  const sessionRevision = useSessionStore((state) => state.sessionRevision);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [loadedRevision, setLoadedRevision] = useState(-1);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [loadError, setLoadError] = useState<{ revision: number; message: string } | null>(null);
  const [retryCount, setRetryCount] = useState(0);
  const runningRef = useRef(false);
  const recoveryNeededRef = useRef(false);
  const setCurrentMessageId = useSessionStore(
    (state) => state.setCurrentMessageId,
  );

  useEffect(() => {
    const controller = new AbortController();
    setEntries([]);
    setError("");
    setLoadError(null);
    setLoadingHistory(true);
    recoveryNeededRef.current = false;

    if (useSessionStore.getState().isNewSession) {
      setCurrentMessageId(null);
      setLoadedRevision(sessionRevision);
      setLoadingHistory(false);
    } else {
      void getSessionHistory(sessionId, controller.signal).then((history) => {
        if (controller.signal.aborted || useSessionStore.getState().sessionRevision !== sessionRevision) return;
        if (!history) throw new Error("会话不存在");
        setCurrentMessageId(history.session.current_message_id);
        setEntries(historyToEntries(history.messages));
        setLoadedRevision(sessionRevision);
        setLoadingHistory(false);
      }).catch((cause: unknown) => {
        if (controller.signal.aborted || useSessionStore.getState().sessionRevision !== sessionRevision) return;
        setLoadError({ revision: sessionRevision, message: cause instanceof Error ? cause.message : String(cause) });
        setLoadingHistory(false);
      });
    }

    return () => controller.abort();
  }, [sessionId, sessionRevision, retryCount, setCurrentMessageId]);

  const historyReady = loadedRevision === sessionRevision && !loadingHistory;
  const historyError = loadError?.revision === sessionRevision ? loadError.message : "";
  const historyBusy = loadingHistory || (loadedRevision !== sessionRevision && !historyError);

  const send = async (message: string, refFileIds: string[] = []) => {
    if (runningRef.current || !historyReady || useSessionStore.getState().sessionRevision !== sessionRevision) {
      return;
    }
    runningRef.current = true;
    setRunning(true);
    setError("");
    const pendingSession = useSessionStore.getState().beginSession(message);
    const userEntryId = crypto.randomUUID();
    const assistantEntryId = crypto.randomUUID();
    const createdAt = Date.now() / 1000;
    setEntries((prev) => [
      ...prev,
      { kind: "user", id: userEntryId, text: message, createdAt },
      { kind: "assistant", id: assistantEntryId, events: [], createdAt },
    ]);
    try {
      const { sessionId } = useSessionStore.getState();
      if (recoveryNeededRef.current) {
        const history = await getSessionHistory(sessionId);
        if (history?.session.current_message_id != null) {
          setCurrentMessageId(history.session.current_message_id);
        }
        recoveryNeededRef.current = false;
      }
      const parentMessageId = useSessionStore.getState().currentMessageId;
      for await (const event of await streamChat({
        session_id: sessionId,
        parent_message_id: parentMessageId,
        message,
        ref_file_ids: refFileIds,
      })) {
        setEntries((prev) => {
          const next = [...prev];
          if (event.type === "user_message") {
            const userIndex = next.findIndex((entry) => entry.id === userEntryId);
            if (userIndex !== -1) {
              const userEntry = next[userIndex];
              if (userEntry.kind === "user") next[userIndex] = { ...userEntry, messageId: event.message_id };
            }
          }
          const lastIndex = next.length - 1;
          const assistantEntry = next[lastIndex];
          if (assistantEntry?.kind === "assistant" && assistantEntry.id === assistantEntryId) {
            next[lastIndex] = {
              ...assistantEntry,
              events: [...assistantEntry.events, event],
              ...(event.type === "done" && event.content
                ? { messageId: event.message_id, createdAt: Date.now() / 1000 }
                : {}),
            };
          }
          return next;
        });
        if (event.type === "error") {
          setError(event.message);
        }
        if (event.type === "user_message" || event.type === "done") {
          setCurrentMessageId(event.message_id);
          recoveryNeededRef.current = false;
        }
      }
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      // 请求可能已在后端创建用户消息，但 SSE 首帧未抵达浏览器；
      // 标记待恢复，下次发送前用历史接口对齐父消息指针。
      recoveryNeededRef.current = true;
      // 失败也要在气泡里留下可见痕迹，不能永远停在「思考中」。
      setEntries((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.kind === "assistant") {
          next[next.length - 1] = {
            ...last,
            events: [...last.events, { type: "error", message }],
          };
        }
        return next;
      });
    } finally {
      runningRef.current = false;
      setRunning(false);
      onConversationChanged();
      const sentSessionId = sessionId;
      const sentRevision = sessionRevision;
      void getSessionHistory(sentSessionId).then((history) => {
        if (useSessionStore.getState().sessionRevision !== sentRevision) return;
        if (!history) {
          if (pendingSession) useSessionStore.getState().discardPendingSession(pendingSession);
          return;
        }
        setEntries((prev) => syncEntryTimestamps(prev, history.messages));
      }).catch(() => {
        // Local timestamps remain visible if the reconciliation request fails.
      });
    }
  };

  return {
    running,
    error,
    entries: loadedRevision === sessionRevision ? entries : [],
    send,
    historyReady,
    loadingHistory: historyBusy,
    historyError,
    retryHistory: () => setRetryCount((count) => count + 1),
  };
}
