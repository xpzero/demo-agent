import { useEffect, useRef } from "react";
import { fetchRunLogs } from "@/adapter/runLog";
import { useRunLogStore } from "@/stores/runLog";
import { Button } from "@/components/shadcn/button";

const POLL_MS = 3000;

const timeFormat = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit", minute: "2-digit", second: "2-digit",
});

function levelStyle(level: "info" | "warn" | "error") {
  switch (level) {
    case "error":
      return "bg-red-500/10 text-red-600 dark:text-red-400";
    case "warn":
      return "bg-amber-500/10 text-amber-600 dark:text-amber-400";
    default:
      return "bg-muted text-muted-foreground";
  }
}

/**
 * 运行日志：右上角按钮切换的持久侧栏面板。
 * 常驻轮询（面板关闭也保持拉取，重开即最新）；开合状态持久（zustand + localStorage）。
 */
export default function RunLogPanel() {
  const open = useRunLogStore((state) => state.open);
  const entries = useRunLogStore((state) => state.entries);
  const lastId = useRunLogStore((state) => state.lastId);
  const toggle = useRunLogStore((state) => state.toggle);
  const push = useRunLogStore((state) => state.push);
  const listRef = useRef<HTMLDivElement>(null);
  const lastIdRef = useRef(lastId);

  useEffect(() => {
    lastIdRef.current = lastId;
  }, [lastId]);

  useEffect(() => {
    const interval = setInterval(() => {
      void fetchRunLogs(lastIdRef.current).then(push).catch(() => {
        // 轮询失败静默——面板是观测辅助，不阻塞主流程
      });
    }, POLL_MS);
    return () => clearInterval(interval);
  }, [push]);

  // 新日志到达时保持滚动贴底（仅面板打开时）
  useEffect(() => {
    const list = listRef.current;
    if (list && open) {
      list.scrollTop = list.scrollHeight;
    }
  }, [entries, open]);

  const errorCount = entries.filter((e) => e.level === "error").length;

  return (
    <>
      <Button
        variant={open ? "secondary" : "outline"}
        size="sm"
        onClick={toggle}
        className="absolute right-4 top-14 z-40 h-7 gap-1.5 bg-background/90 px-2 text-xs shadow-sm backdrop-blur"
      >
        <span className="relative flex size-2">
          <span className="absolute inline-flex size-2 animate-ping rounded-full bg-emerald-400 opacity-60" />
          <span className="relative inline-flex size-2 rounded-full bg-emerald-500" />
        </span>
        运行日志
        {errorCount > 0 && (
          <span className="rounded-full bg-red-500/15 px-1.5 text-[10px] font-medium text-red-600">
            {errorCount}
          </span>
        )}
      </Button>
      {open && (
        <aside className="absolute inset-y-0 right-0 z-30 flex w-80 flex-col border-l bg-popover/95 shadow-xl backdrop-blur-sm">
          <header className="flex h-12 shrink-0 items-center justify-between border-b px-4">
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              运行日志
              <span className="text-xs font-normal text-muted-foreground">
                {entries.length} 条 · 每 3s
              </span>
            </h2>
          </header>
          <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto px-2 py-2 font-mono text-xs">
            {entries.length === 0 && (
              <p className="p-4 text-center text-muted-foreground">暂无日志</p>
            )}
            {entries.map((entry) => (
              <div
                key={entry.id}
                className="flex items-start gap-2 rounded-md px-2 py-1.5 leading-6 hover:bg-muted/50"
              >
                <span className="shrink-0 tabular-nums text-muted-foreground">
                  {timeFormat.format(new Date(entry.ts * 1000))}
                </span>
                <span className={`shrink-0 rounded px-1.5 text-[10px] font-semibold uppercase leading-6 ${levelStyle(entry.level)}`}>
                  {entry.source}
                </span>
                <span className="min-w-0 break-all">
                  {entry.message}
                  {entry.message_id !== undefined && (
                    <span className="text-muted-foreground"> #{entry.message_id}</span>
                  )}
                  {entry.chars !== undefined && (
                    <span className="text-muted-foreground"> · {entry.chars}字</span>
                  )}
                </span>
              </div>
            ))}
          </div>
        </aside>
      )}
    </>
  );
}
