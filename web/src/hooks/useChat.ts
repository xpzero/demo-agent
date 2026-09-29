import { useEffect, useRef, useState } from "react";
import { getSessionHistory, getSessionStatus, HttpError, stopSession, streamChat } from "@/adapter";
import { useSessionStore } from "@/stores/session";
import { historyToMessages, reconcileTurnFromHistory, syncMessageTimestamps } from "@/chat/messageHistory";
import { recoverSession, recoveryPollInterval } from "@/chat/sessionRecovery";
import { appendPendingTurn, applyChatEvent, type TurnIds } from "@/chat/messageUpdates";
import { useSessionMessages } from "./useSessionMessages";

export function useChat(onConversationChanged: () => void) {
  const sessionId = useSessionStore((state) => state.sessionId);
  const sessionGeneration = useSessionStore((state) => state.sessionGeneration);
  const [running, setRunning] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState("");
  const [statusMessage, setStatusMessage] = useState("");
  const runningRef = useRef(false);
  const stoppingRef = useRef(false);
  const streamController = useRef<AbortController | null>(null);
  const changedRef = useRef(onConversationChanged);
  changedRef.current = onConversationChanged;
  const setCurrentMessageId = useSessionStore((state) => state.setCurrentMessageId);
  const {
    messages, setMessages, summaryCursor, setSummaryCursor, canSend, setCanSend,
    historyReady, loadingHistory, historyError, retryHistory,
  } = useSessionMessages(sessionId, sessionGeneration);

  useEffect(() => {
    setError("");
    setStatusMessage("");
    return () => streamController.current?.abort();
  }, [sessionGeneration]);

  // 断流或打开仍在处理的历史会话时，持续检查状态；网络失败不解除发送保护。
  useEffect(() => {
    if (!historyReady || canSend || running) {
      return;
    }
    let disposed = false;
    let finished = false;
    let inFlight = false;
    let requestController: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const interval = recoveryPollInterval(import.meta.env.VITE_SESSION_STATUS_POLL_MS);
    const available = () => document.visibilityState === "visible" && navigator.onLine;
    const poll = async () => {
      if (disposed || finished || inFlight || !available()) {
        return;
      }
      inFlight = true;
      const controller = new AbortController();
      requestController = controller;
      try {
        const result = await recoverSession(sessionId, controller.signal);
        if (disposed || controller.signal.aborted || useSessionStore.getState().sessionGeneration !== sessionGeneration) {
          return;
        }
        if (result.ready) {
          if (result.history) {
            setCurrentMessageId(result.history.session.current_message_id);
            setSummaryCursor(result.history.session.summary_upto_message_id);
            setMessages(historyToMessages(result.history.messages));
          } else if (useSessionStore.getState().isNewSession) {
            // 本页尚未建立会话（新会话首条发送在落库前失败），可重新发送。
            const pending = useSessionStore.getState().pendingSession;
            if (pending) {
              useSessionStore.getState().discardPendingSession(pending);
            }
            setCurrentMessageId(null);
          } else {
            // 已存在会话却返回 404：不能据此解锁或清空既有会话，保持锁定并继续重试。
            setStatusMessage("会话在服务端查询不存在，已保持发送锁定，将继续自动重试…");
            return;
          }
          finished = true;
          setCanSend(true);
          setStatusMessage("");
          changedRef.current();
          return;
        }
        setStatusMessage("会话正在处理，等待完成后恢复消息…");
      } catch (cause) {
        if (disposed || controller.signal.aborted) {
          return;
        }
        setStatusMessage(`恢复连接失败，将自动重试：${cause instanceof Error ? cause.message : String(cause)}`);
      } finally {
        inFlight = false;
        requestController = null;
        if (!disposed && !finished && available()) {
          timer = setTimeout(() => { void poll(); }, interval);
        }
      }
    };
    const availabilityChanged = () => {
      clearTimeout(timer);
      if (!available()) {
        requestController?.abort();
        return;
      }
      void poll();
    };
    document.addEventListener("visibilitychange", availabilityChanged);
    window.addEventListener("online", availabilityChanged);
    window.addEventListener("offline", availabilityChanged);
    void poll();
    return () => {
      disposed = true;
      requestController?.abort();
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", availabilityChanged);
      window.removeEventListener("online", availabilityChanged);
      window.removeEventListener("offline", availabilityChanged);
    };
  }, [sessionId, sessionGeneration, historyReady, canSend, running, setCanSend, setMessages, setSummaryCursor, setCurrentMessageId]);

  const send = async (message: string, refFileIds: string[] = [], onAccepted: () => void = () => {}) => {
    if (runningRef.current || stoppingRef.current || !canSend || useSessionStore.getState().sessionGeneration !== sessionGeneration) {
      return;
    }
    runningRef.current = true;
    setRunning(true);
    setCanSend(false);
    setError("");
    setStatusMessage("");
    const pendingSession = useSessionStore.getState().beginSession(message);
    const controller = new AbortController();
    streamController.current = controller;
    const ids: TurnIds = { userId: crypto.randomUUID(), assistantId: crypto.randomUUID() };
    let accepted = false;
    let completed = false;
    const current = () => !controller.signal.aborted && useSessionStore.getState().sessionGeneration === sessionGeneration;
    const reconcile = async () => {
      const history = await getSessionHistory(sessionId, controller.signal);
      if (!current() || !history) {
        return null;
      }
      setCurrentMessageId(history.session.current_message_id);
      setSummaryCursor(history.session.summary_upto_message_id);
      setMessages((prev) => syncMessageTimestamps(reconcileTurnFromHistory(prev, history.messages, ids), history.messages));
      return history;
    };
    const refreshCanSend = async () => {
      const status = await getSessionStatus(sessionId, controller.signal);
      if (!current()) {
        return;
      }
      setCanSend(status ? status.can_send_message : false);
      if (status && !status.can_send_message) {
        setStatusMessage("会话正在处理，等待完成后恢复消息…");
      }
    };
    try {
      const events = await streamChat({
        session_id: sessionId,
        parent_message_id: useSessionStore.getState().currentMessageId,
        message,
        ref_file_ids: refFileIds,
      }, controller.signal);
      if (!current()) {
        return;
      }
      accepted = true;
      onAccepted();
      setMessages((prev) => appendPendingTurn(prev, message, ids, Date.now() / 1000));
      for await (const event of events) {
        if (!current()) {
          return;
        }
        setMessages((prev) => applyChatEvent(prev, event, ids, Date.now() / 1000));
        if (event.type === "error") {
          setError(event.message);
        }
        if (event.type === "user_message" || event.type === "done") {
          setCurrentMessageId(event.message_id);
        }
      }
      completed = true;
      // 终止事件后以服务端落库事实为准：正文、完成状态、工具存档与发送限制。
      const history = await reconcile();
      if (current()) {
        if (history) {
          setCanSend(history.can_send_message);
        } else {
          await refreshCanSend();
        }
      }
    } catch (cause) {
      if (current()) {
        const message = cause instanceof Error ? cause.message : String(cause);
        setError(message);
        if (cause instanceof HttpError && cause.status === 409 && cause.code === "session_busy") {
          // 会话正被其他请求占用：保留 session_busy 语义，刷新状态而不是解锁。
          try {
            await refreshCanSend();
          } catch {
            setCanSend(false);
          }
        } else {
          if (accepted && !completed) {
            setMessages((prev) => applyChatEvent(prev, { type: "error", message }, ids, Date.now() / 1000));
          }
          if (pendingSession && !accepted) {
            useSessionStore.getState().discardPendingSession(pendingSession);
          }
        }
      }
    } finally {
      if (streamController.current === controller) {
        streamController.current = null;
        runningRef.current = false;
        setRunning(false);
      }
      changedRef.current();
    }
  };

  const stop = async () => {
    if (stoppingRef.current) {
      return;
    }
    stoppingRef.current = true;
    setStopping(true);
    setError("");
    try {
      const result = await stopSession(sessionId);
      if (useSessionStore.getState().sessionGeneration !== sessionGeneration) {
        return;
      }
      setStatusMessage(result.result === "processing" ? "已请求停止，等待当前任务结束…" : "任务已结束，正在同步消息…");
      // 停止请求已提交，关闭本地消费并由状态轮询等待服务端真正结束。
      streamController.current?.abort();
      setCanSend(false);
    } catch (cause) {
      if (useSessionStore.getState().sessionGeneration === sessionGeneration) {
        setError(`停止失败：${cause instanceof Error ? cause.message : String(cause)}`);
      }
    } finally {
      stoppingRef.current = false;
      setStopping(false);
    }
  };

  return {
    running, stopping, stop, canSend, statusMessage, error, messages, summaryCursor,
    send, historyReady, loadingHistory, historyError,
    retryHistory: () => { setError(""); retryHistory(); },
  };
}
