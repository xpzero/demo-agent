import { create } from "zustand";
import type { RunLogEntry } from "@/adapter/runLog";

const MAX_DISPLAY = 100;

type RunLogState = {
  open: boolean;
  entries: RunLogEntry[];
  lastId: number;
  toggle: () => void;
  push: (fresh: RunLogEntry[]) => void;
};

/** 运行日志面板状态（开合经 localStorage 持久化；条目存 store，面板卸载也不丢）。 */
const OPEN_KEY = "demo-agent:run-log-open";

function restoreOpen(): boolean {
  try {
    return localStorage.getItem(OPEN_KEY) === "1";
  } catch {
    return false;
  }
}

export const useRunLogStore = create<RunLogState>((set) => ({
  open: restoreOpen(),
  entries: [],
  lastId: 0,
  toggle: () =>
    set((state) => {
      const open = !state.open;
      try {
        localStorage.setItem(OPEN_KEY, open ? "1" : "0");
      } catch {
        // 持久化失败忽略
      }
      return { open };
    }),
  push: (fresh) => {
    if (fresh.length === 0) {
      return;
    }
    set((state) => ({
      lastId: fresh[fresh.length - 1].id,
      entries: [...state.entries, ...fresh].slice(-MAX_DISPLAY),
    }));
  },
}));
