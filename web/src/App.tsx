import UserInput from "@/components/ui/user-input";
import ChatMessages from "@/components/ui/chat-messages";
import AppSidebar from "@/components/ui/app-sidebar";
import SessionStatsBadge from "@/components/ui/session-stats-badge";
import { Button } from "@/components/shadcn/button";
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/shadcn/sidebar";
import { TooltipProvider } from "@/components/shadcn/tooltip";
import { useUserInputStore } from "@/stores";
import { useSessionStore } from "@/stores/session";
import { useChat } from "@/hooks/useChat";
import { useSessions } from "@/hooks/useSessions";
import { useSessionStats } from "@/hooks/useSessionStats";

export default function App() {
  const clear = useUserInputStore((state) => state.clear);
  const sessionGeneration = useSessionStore((state) => state.sessionGeneration);
  const { sessions, loading, error: sessionsError, refresh } = useSessions();
  const { stats, refreshStats } = useSessionStats();
  const {
    running, error, messages, summaryCursor, send, historyReady, loadingHistory, historyError, retryHistory,
  } = useChat(() => {
    void refresh();
    refreshStats();
  });
  const busy = running || loadingHistory;

  const selectSession = (id: string) => {
    if (busy) {
      return;
    }
    clear();
    useSessionStore.getState().switchSession(id);
  };

  const newSession = () => {
    if (busy) {
      return;
    }
    clear();
    useSessionStore.getState().newSession();
  };

  return (
    <TooltipProvider>
      <SidebarProvider className="h-svh">
        <AppSidebar
          sessions={sessions}
          loading={loading}
          error={sessionsError}
          busy={busy}
          onSelectSession={selectSession}
          onNewSession={newSession}
          onRefresh={() => { void refresh(); }}
        />
        <SidebarInset className="h-full min-h-0 min-w-0 overflow-hidden">
          <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4">
            <SidebarTrigger />
            <h1 className="text-sm font-semibold">Agent Demo</h1>
            <SessionStatsBadge stats={stats} />
          </header>

          <main className="flex min-h-0 flex-1 flex-col items-center gap-4 overflow-hidden py-8">
            <div className="flex min-h-0 w-full flex-1 flex-col overflow-hidden">
              {loadingHistory ? (
                <p className="text-sm text-muted-foreground">加载会话中…</p>
              ) : (
                <ChatMessages key={sessionGeneration} messages={messages} summaryCursor={summaryCursor} />
              )}
            </div>
            {historyError && (
              <div className="flex items-center gap-2 text-sm text-red-500">
                <span>加载会话失败：{historyError}</span>
                <Button variant="ghost" size="sm" onClick={retryHistory}>重试</Button>
              </div>
            )}
            {error && <p className="text-sm text-red-500">{error}</p>}

            <div className="flex w-full shrink-0 justify-center">
              <UserInput
                key={sessionGeneration}
                onSend={send}
                running={running || !historyReady}
                historyReady={historyReady}
              />
            </div>
          </main>
        </SidebarInset>
      </SidebarProvider>
    </TooltipProvider>
  );
}
