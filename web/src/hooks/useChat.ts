import { useEffect, useRef, useState } from "react";
import { getSessionHistory, HttpError, stopSession, streamChat, type SessionProgress } from "@/adapter";
import { useSessionStore } from "@/stores/session";
import { calibrateHistory } from "@/chat/messageHistory";
import { needsResume, recoverSession, resumeSnapshot } from "@/chat/sessionRecovery";
import { appendPendingTurn, applyChatEvent, type TurnIds } from "@/chat/messageUpdates";
import { useSessionMessages } from "./useSessionMessages";

type Connection = { controller: AbortController; mode: "chat" | "resume"; generation: number };

export function useChat(onConversationChanged: () => void) {
  const sessionId = useSessionStore((state) => state.sessionId);
  const sessionGeneration = useSessionStore((state) => state.sessionGeneration);
  const [running, setRunning] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState("");
  const [statusMessage, setStatusMessage] = useState("");
  const [progress, setProgress] = useState<SessionProgress>();
  const [resumeRequest, setResumeRequest] = useState(0);
  const connection = useRef<Connection | null>(null);
  const requestedGeneration = useRef<number | null>(null);
  const stoppingRef = useRef(false);
  const changedRef = useRef(onConversationChanged);
  changedRef.current = onConversationChanged;
  const setCurrentMessageId = useSessionStore((state) => state.setCurrentMessageId);
  const {
    messages, setMessages, summaryCursor, setSummaryCursor, canSend, setCanSend,
    historyReady, initialHistory, loadingHistory, historyError, retryHistory,
  } = useSessionMessages(sessionId, sessionGeneration);

  const requestResume = () => {
    requestedGeneration.current = sessionGeneration;
    setCanSend(false);
    setResumeRequest((value) => value + 1);
  };

  useEffect(() => {
    setRunning(false);
    setStopping(false);
    stoppingRef.current = false;
    setError("");
    setStatusMessage("");
    setProgress(undefined);
    return () => {
      connection.current?.controller.abort();
      connection.current = null;
    };
  }, [sessionGeneration]);

  // 只由历史加载或显式恢复请求触发；恢复失败不会因为 loading/canSend 改变而循环请求。
  useEffect(() => {
    if (!historyReady) {
      return;
    }
    setProgress(initialHistory?.progress);
    if (requestedGeneration.current !== sessionGeneration && (!initialHistory || !needsResume(initialHistory))) {
      return;
    }
    if (connection.current) {
      return;
    }
    const controller = new AbortController();
    const owner: Connection = { controller, mode: "resume", generation: sessionGeneration };
    connection.current = owner;
    let ids: TurnIds | null = null;
    const current = () => !controller.signal.aborted && connection.current === owner && useSessionStore.getState().sessionGeneration === sessionGeneration;
    setRunning(true);
    setCanSend(false);
    setStatusMessage("正在恢复会话…");
    void recoverSession(sessionId, controller.signal, (event) => {
      if (!current()) {
        return;
      }
      if (event.type === "resume_snapshot") {
        const recovered = resumeSnapshot(event);
        ids = recovered.ids;
        setMessages(recovered.messages);
        setCurrentMessageId(event.history.session.current_message_id);
        setSummaryCursor(event.history.session.summary_upto_message_id);
        setProgress(event.history.progress);
        setStatusMessage(event.history.can_send_message ? "正在同步消息…" : "会话正在处理…");
      } else if (event.type !== "stream_end" && ids) {
        const target = ids;
        setMessages((prev) => applyChatEvent(prev, event, target, Date.now() / 1000));
        if (event.type === "error") {
          setError(event.message);
        }
      }
    }, (history) => {
      if (!current()) {
        return;
      }
      if (history) {
        const target = ids;
        setMessages((prev) => calibrateHistory(prev, history.messages, target));
        setCurrentMessageId(history.session.current_message_id);
        setSummaryCursor(history.session.summary_upto_message_id);
        setProgress(history.progress);
        setCanSend(history.can_send_message);
      } else {
        setCurrentMessageId(null);
        setCanSend(true);
      }
    }, useSessionStore.getState().isNewSession).then(() => {
      if (current()) {
        setStatusMessage("");
        changedRef.current();
      }
    }).catch(() => {
      if (current()) {
        setCanSend(false);
        setError("连接中断，请刷新重试");
        setStatusMessage("");
      }
    }).finally(() => {
      if (connection.current === owner) {
        connection.current = null;
        setRunning(false);
      }
    });
    return () => {
      controller.abort();
      if (connection.current === owner) {
        connection.current = null;
      }
    };
  }, [sessionId, sessionGeneration, historyReady, initialHistory, resumeRequest, setCanSend, setMessages, setSummaryCursor, setCurrentMessageId]);

  const send = async (message: string, refFileIds: string[] = [], onAccepted: () => void = () => {}) => {
    if (connection.current || stoppingRef.current || !canSend || useSessionStore.getState().sessionGeneration !== sessionGeneration) {
      return;
    }
    const controller = new AbortController();
    const owner: Connection = { controller, mode: "chat", generation: sessionGeneration };
    connection.current = owner;
    setRunning(true);
    setCanSend(false);
    setError("");
    setStatusMessage("");
    const pending = useSessionStore.getState().beginSession(message);
    const ids: TurnIds = { userId: crypto.randomUUID(), assistantId: crypto.randomUUID() };
    const current = () => !controller.signal.aborted && connection.current === owner && useSessionStore.getState().sessionGeneration === sessionGeneration;
    let resume = false;
    try {
      const events = await streamChat({ session_id: sessionId, parent_message_id: useSessionStore.getState().currentMessageId, message, ref_file_ids: refFileIds }, controller.signal);
      if (!current()) {
        return;
      }
      onAccepted();
      // 接受时已持久化会话，立即刷新列表；生成中切走后仍能从侧边栏切回。
      changedRef.current();
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
      const history = await getSessionHistory(sessionId, controller.signal);
      if (current()) {
        if (!history) {
          resume = true;
        } else {
          setMessages((prev) => calibrateHistory(prev, history.messages, ids));
          setCurrentMessageId(history.session.current_message_id);
          setSummaryCursor(history.session.summary_upto_message_id);
          setProgress(history.progress);
          setCanSend(history.can_send_message);
          resume = !history.can_send_message;
        }
      }
    } catch (cause) {
      if (current()) {
        if (cause instanceof HttpError && cause.status !== 409 && cause.status < 500) {
          setError(cause.message);
          if (pending) {
            useSessionStore.getState().discardPendingSession(pending);
          }
          setCanSend(true);
        } else {
          // 网络失败可能已被服务端接收；只读恢复，不重发用户消息。
          resume = true;
        }
      }
    } finally {
      if (connection.current === owner) {
        connection.current = null;
        setRunning(false);
        if (resume) {
          requestResume();
        }
        changedRef.current();
      }
    }
  };

  const stop = async () => {
    if (stoppingRef.current) {
      return;
    }
    stoppingRef.current = true;
    setStopping(true);
    const stopController = new AbortController();
    try {
      const result = await stopSession(sessionId, stopController.signal);
      if (useSessionStore.getState().sessionGeneration === sessionGeneration) {
        setStatusMessage(result.result === "processing" ? "已请求停止，正在收尾…" : "正在同步停止结果…");
      }
    } catch (cause) {
      if (useSessionStore.getState().sessionGeneration === sessionGeneration) {
        setError(`停止请求未确认：${cause instanceof Error ? cause.message : String(cause)}`);
      }
    } finally {
      if (useSessionStore.getState().sessionGeneration === sessionGeneration) {
        stoppingRef.current = false;
        setStopping(false);
        if (connection.current?.mode !== "resume") {
          connection.current?.controller.abort();
          connection.current = null;
          setRunning(false);
          requestResume();
        }
      }
    }
  };

  return { running, stopping, stop, canSend, statusMessage, error, progress, messages, summaryCursor, send, historyReady, loadingHistory, historyError,
    retryHistory: () => { setError(""); retryHistory(); } };
}
