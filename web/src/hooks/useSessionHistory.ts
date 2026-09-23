import { useEffect, useState } from "react";
import { getSessionHistory, type SessionHistory } from "@/adapter";
import { useSessionStore } from "@/stores/session";

type HistoryState = {
  revision: number;
  status: "loading" | "ready" | "error";
  error: string;
};

export function useSessionHistory({
  sessionId,
  sessionRevision,
  onReset,
  onLoaded,
}: {
  sessionId: string;
  sessionRevision: number;
  onReset: () => void;
  onLoaded: (history: SessionHistory | null) => void;
}) {
  const [state, setState] = useState<HistoryState>({ revision: -1, status: "loading", error: "" });
  const [retryCount, setRetryCount] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    onReset();
    setState({ revision: sessionRevision, status: "loading", error: "" });

    if (useSessionStore.getState().isNewSession) {
      onLoaded(null);
      setState({ revision: sessionRevision, status: "ready", error: "" });
    } else {
      void getSessionHistory(sessionId, controller.signal).then((history) => {
        if (controller.signal.aborted || useSessionStore.getState().sessionRevision !== sessionRevision) return;
        if (!history) throw new Error("会话不存在");
        onLoaded(history);
        setState({ revision: sessionRevision, status: "ready", error: "" });
      }).catch((cause: unknown) => {
        if (controller.signal.aborted || useSessionStore.getState().sessionRevision !== sessionRevision) return;
        setState({
          revision: sessionRevision,
          status: "error",
          error: cause instanceof Error ? cause.message : String(cause),
        });
      });
    }

    return () => controller.abort();
  }, [sessionId, sessionRevision, retryCount, onReset, onLoaded]);

  const current = state.revision === sessionRevision;
  return {
    historyReady: current && state.status === "ready",
    loadingHistory: !current || state.status === "loading",
    historyError: current && state.status === "error" ? state.error : "",
    retryHistory: () => setRetryCount((count) => count + 1),
  };
}
