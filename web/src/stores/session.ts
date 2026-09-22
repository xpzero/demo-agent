import { create } from "zustand";

type SessionState = {
  sessionId: string;
  currentMessageId: number | null;
  setCurrentMessageId: (id: number) => void;
  newSession: () => void;
};

export const useSessionStore = create<SessionState>((set) => ({
  sessionId: crypto.randomUUID(),
  currentMessageId: null,
  setCurrentMessageId: (id) => set({ currentMessageId: id }),
  newSession: () => set({ sessionId: crypto.randomUUID(), currentMessageId: null }),
}));
