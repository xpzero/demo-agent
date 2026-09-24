import { useCallback, useEffect, useRef, useState } from "react";
import { getSessionStats, type SessionStats } from "@/adapter";
import { useSessionStore } from "@/stores/session";

type StatsState = {
  generation: number;
  data: SessionStats | null;
};

/** 会话观测汇总：会话切换时拉取，聊天结束后刷新。 */
export function useSessionStats() {
  const sessionId = useSessionStore((store) => store.sessionId);
  const sessionGeneration = useSessionStore((store) => store.sessionGeneration);
  const isNewSession = useSessionStore((store) => store.isNewSession);
  const [state, setState] = useState<StatsState>({ generation: -1, data: null });
  const requestRef = useRef<AbortController | null>(null);

  // useChat 的 send 会持有开始发送时的回调，必须在调用时读取最新会话状态。
  const refreshStats = useCallback(() => {
    requestRef.current?.abort();
    const { sessionId, sessionGeneration, isNewSession } = useSessionStore.getState();
    if (isNewSession) {
      setState({ generation: sessionGeneration, data: null });
      return;
    }
    const controller = new AbortController();
    requestRef.current = controller;
    void getSessionStats(sessionId, controller.signal)
      .then((data) => {
        const current = useSessionStore.getState();
        if (!controller.signal.aborted &&
            current.sessionId === sessionId &&
            current.sessionGeneration === sessionGeneration) {
          setState({ generation: sessionGeneration, data });
        }
      })
      .catch(() => {
        // 观测数据是旁路信息，失败不阻塞聊天界面。
      });
  }, []);

  useEffect(() => {
    refreshStats();
    return () => requestRef.current?.abort();
  }, [sessionId, sessionGeneration, isNewSession, refreshStats]);

  return {
    stats: state.generation === sessionGeneration ? state.data : null,
    refreshStats,
  };
}
