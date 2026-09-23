import { Bot, MessageSquare, Plus, RotateCw } from "lucide-react";
import type { SessionSummary } from "@/adapter";
import { useSessionStore } from "@/stores/session";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
  SidebarRail,
  useSidebar,
} from "@/components/shadcn/sidebar";

type AppSidebarProps = {
  sessions: SessionSummary[];
  loading: boolean;
  error: string;
  busy: boolean;
  onSelectSession: (id: string) => void;
  onNewSession: () => void;
  onRefresh: () => void;
};

export default function AppSidebar({
  sessions, loading, error, busy, onSelectSession, onNewSession, onRefresh,
}: AppSidebarProps) {
  const sessionId = useSessionStore((state) => state.sessionId);
  const isNewSession = useSessionStore((state) => state.isNewSession);
  const pendingSession = useSessionStore((state) => state.pendingSession);
  const showPendingSession = pendingSession?.id === sessionId && !sessions.some((session) => session.id === sessionId);
  const { isMobile, setOpenMobile } = useSidebar();

  const selectSession = (id: string) => {
    if (busy || id === sessionId) return;
    onSelectSession(id);
    if (isMobile) setOpenMobile(false);
  };

  return (
    <Sidebar>
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" className="pointer-events-none">
              <div className="flex size-8 items-center justify-center rounded-md bg-sidebar-primary text-sidebar-primary-foreground">
                <Bot className="size-4" />
              </div>
              <span className="text-base font-semibold">Agent Demo</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              disabled={busy || isNewSession}
              tooltip="新会话"
              onClick={() => {
                onNewSession();
                if (isMobile) setOpenMobile(false);
              }}
            >
              <Plus />
              <span>新会话</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>会话</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {showPendingSession && pendingSession && (
                <SidebarMenuItem key={sessionId}>
                  <SidebarMenuButton isActive tooltip={pendingSession.title} aria-current="true" disabled>
                    <MessageSquare />
                    <span className="min-w-0 truncate">{pendingSession.title}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              )}
              {loading && sessions.length === 0 && !error && Array.from({ length: 3 }, (_, index) => (
                <SidebarMenuItem key={index}>
                  <SidebarMenuSkeleton showIcon />
                </SidebarMenuItem>
              ))}
              {sessions.map((session) => (
                <SidebarMenuItem key={session.id}>
                  <SidebarMenuButton
                    isActive={session.id === sessionId}
                    aria-current={session.id === sessionId ? "true" : undefined}
                    tooltip={session.title}
                    disabled={busy}
                    onClick={() => selectSession(session.id)}
                  >
                    <MessageSquare />
                    <span className="min-w-0 truncate">{session.title || "新对话"}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
              {!loading && sessions.length === 0 && !showPendingSession && !error && (
                <li className="px-2 py-2 text-xs text-sidebar-foreground/60">暂无历史会话</li>
              )}
              {error && (
                <SidebarMenuItem>
                  <SidebarMenuButton tooltip={error} onClick={onRefresh}>
                    <RotateCw />
                    <span>加载失败，点击重试</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              )}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarRail />
    </Sidebar>
  );
}
