import { useCallback, useEffect, useState } from "react";
import { getSessionStats, type SessionStats } from "@/adapter";
import { useSessionStore } from "@/stores/session";

type StatsState = {
  generation: number;
  data: SessionStats | null;
};

/** 会话观测汇总的唯一所有者：会话切换时拉取，聊天结束后可手动刷新。 */
export function useSessionStats() {
  const sessionId = useSessionStore((store) => store.sessionId);
  const sessionGeneration = useSessionStore((store) => store.sessionGeneration);
  const isNewSession = useSessionStore((store) => store.isNewSession);
  const [state, setState] = useState<StatsState>({ generation: -1, data: null });

  useEffect(() => {
    const controller = new AbortController();
    setState({ generation: sessionGeneration, data: null });
    if (!isNewSession) {
      void getSessionStats(sessionId, controller.signal)
        .then((data) => {
          if (controller.signal.aborted) {
            return;
          }
          setState({ generation: sessionGeneration, data });
        })
        .catch(() => {
          // 观测数据是旁路信息，失败静默：不阻塞聊天界面
        });
    }
    return () => controller.abort();
  }, [sessionId, sessionGeneration, isNewSession]);

  const refresh = useCallback(() => {
    if (isNewSession) {
      return;
    }
    const controller = new AbortController();
    void getSessionStats(sessionId, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) {
          setState({ generation: sessionGeneration, data });
        }
      })
      .catch(() => {
        // 同上：旁路信息，失败静默
      });
    return () => controller.abort();
  }, [sessionId, sessionGeneration, isNewSession]);

  const current = state.generation === sessionGeneration;
  return {
    stats: current ? state.data : null,
    refreshStats: refresh,
  };
}
