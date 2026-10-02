import { create } from "zustand";

type PendingSession = { id: string; title: string };

type SessionState = {
  sessionId: string;
  pendingSession: PendingSession | null;
  /** 前端会话视图代次；切换时递增，用于丢弃旧请求结果，不是服务端修订号。 */
  sessionGeneration: number;
  currentMessageId: number | null;
  isNewSession: boolean;
  setCurrentMessageId: (id: number | null) => void;
  beginSession: (message: string) => PendingSession | null;
  discardPendingSession: (pending: PendingSession) => void;
  switchSession: (id: string) => void;
  newSession: () => void;
};

const LAST_SESSION_KEY = "demo-agent:last-session";

/**
 * 刷新保持会话：beforeunload 写入当前 sessionId，重载时读回。
 * 「新会话」按钮与首次访问（无记录）不恢复。
 */
function restoreSession(): { sessionId: string; isNewSession: boolean } {
  try {
    const saved = localStorage.getItem(LAST_SESSION_KEY);
    if (saved) {
      localStorage.removeItem(LAST_SESSION_KEY);
      return { sessionId: saved, isNewSession: false };
    }
  } catch {
    // localStorage 不可用时退回新建
  }
  return { sessionId: crypto.randomUUID(), isNewSession: true };
}

function persistSession(sessionId: string, isNewSession: boolean) {
  try {
    if (isNewSession) {
      localStorage.removeItem(LAST_SESSION_KEY);
    } else {
      localStorage.setItem(LAST_SESSION_KEY, sessionId);
    }
  } catch {
    // 忽略持久化失败
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("beforeunload", () => {
    const state = useSessionStore.getState();
    persistSession(state.sessionId, state.isNewSession);
  });
}

export const useSessionStore = create<SessionState>((set, get) => {
  const { sessionId, isNewSession } = restoreSession();
  return {
    sessionId,
    pendingSession: null,
    sessionGeneration: 0,
    currentMessageId: null,
    isNewSession,
    setCurrentMessageId: (id) => set((state) => ({
      currentMessageId: id,
      isNewSession: id === null ? state.isNewSession : false,
    })),
    beginSession: (message) => {
      if (!get().isNewSession) {
        return null;
      }
      const pending = { id: get().sessionId, title: message.trim().slice(0, 30) || "新对话" };
      set({ pendingSession: pending });
      return pending;
    },
    discardPendingSession: (pending) => set((state) => ({
      pendingSession: state.pendingSession === pending ? null : state.pendingSession,
    })),
    switchSession: (id) => set((state) => ({
      sessionId: id,
      pendingSession: null,
      sessionGeneration: state.sessionGeneration + 1,
      currentMessageId: null,
      isNewSession: false,
    })),
    newSession: () => set((state) => {
      const sessionId = crypto.randomUUID();
      return {
        sessionId,
        pendingSession: null,
        sessionGeneration: state.sessionGeneration + 1,
        currentMessageId: null,
        isNewSession: true,
      };
    }),
  };
});
