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

export const useSessionStore = create<SessionState>((set, get) => {
  const sessionId = crypto.randomUUID();
  return {
    sessionId,
    pendingSession: null,
    sessionGeneration: 0,
    currentMessageId: null,
    isNewSession: true,
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
