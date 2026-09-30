import { create } from "zustand";

type PendingSession = { id: string; title: string };

const ACTIVE_SESSION_KEY = "demo-agent.active-session";
const PENDING_SESSION_KEY = "demo-agent.pending-session";

function markPending(pending: boolean) {
  try {
    if (pending) {
      window.sessionStorage.setItem(PENDING_SESSION_KEY, "1");
    } else {
      window.sessionStorage.removeItem(PENDING_SESSION_KEY);
    }
  } catch {
    // 标签页存储不可用时仍可继续当前会话。
  }
}

function restoredPending(): boolean {
  try {
    return window.sessionStorage.getItem(PENDING_SESSION_KEY) === "1";
  } catch {
    return false;
  }
}

function rememberSession(id: string | null) {
  try {
    if (id === null) {
      window.sessionStorage.removeItem(ACTIVE_SESSION_KEY);
    } else {
      window.sessionStorage.setItem(ACTIVE_SESSION_KEY, id);
    }
  } catch {
    // Private browsing may reject storage; the current tab still works in memory.
  }
}

function restoredSession(): string | null {
  try {
    const value = window.sessionStorage.getItem(ACTIVE_SESSION_KEY);
    return value && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value) ? value : null;
  } catch {
    return null;
  }
}

type SessionState = {
  sessionId: string;
  pendingSession: PendingSession | null;
  /** 前端会话视图代次；切换时递增，用于丢弃旧请求结果，不是服务端修订号。 */
  sessionGeneration: number;
  currentMessageId: number | null;
  isNewSession: boolean;
  restorePending: boolean;
  setCurrentMessageId: (id: number | null) => void;
  beginSession: (message: string) => PendingSession | null;
  discardPendingSession: (pending: PendingSession) => void;
  switchSession: (id: string) => void;
  newSession: () => void;
};

export const useSessionStore = create<SessionState>((set, get) => {
  const restored = restoredSession();
  const sessionId = restored ?? crypto.randomUUID();
  return {
    sessionId,
    pendingSession: null,
    sessionGeneration: 0,
    currentMessageId: null,
    isNewSession: restored === null || restoredPending(),
    restorePending: restored !== null && restoredPending(),
    setCurrentMessageId: (id) => {
      if (id !== null) {
        rememberSession(get().sessionId);
        markPending(false);
      }
      if (id === null && get().restorePending) {
        rememberSession(null);
        markPending(false);
      }
      set((state) => ({
        restorePending: false,
        currentMessageId: id,
        isNewSession: id === null ? state.isNewSession : false,
      }));
    },
    beginSession: (message) => {
      if (!get().isNewSession) {
        return null;
      }
      const pending = { id: get().sessionId, title: message.trim().slice(0, 30) || "新对话" };
      rememberSession(pending.id);
      markPending(true);
      set({ pendingSession: pending });
      return pending;
    },
    discardPendingSession: (pending) => {
      if (get().pendingSession === pending && get().isNewSession) {
        rememberSession(null);
        markPending(false);
      }
      set((state) => ({ pendingSession: state.pendingSession === pending ? null : state.pendingSession }));
    },
    switchSession: (id) => {
      rememberSession(id);
      markPending(false);
      set((state) => ({
        sessionId: id,
        pendingSession: null,
        sessionGeneration: state.sessionGeneration + 1,
        currentMessageId: null,
        isNewSession: false,
        restorePending: false,
      }));
    },
    newSession: () => set((state) => {
      rememberSession(null);
      markPending(false);
      const sessionId = crypto.randomUUID();
      return {
        sessionId,
        pendingSession: null,
        sessionGeneration: state.sessionGeneration + 1,
        currentMessageId: null,
        isNewSession: true,
        restorePending: false,
      };
    }),
  };
});
