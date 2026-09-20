import UserInput from "@/components/ui/user-input";
import ChatMessages from "@/components/ui/chat-messages";
import AppSidebar from "@/components/ui/app-sidebar";
import { Button } from "@/components/shadcn/button";
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/shadcn/sidebar";
import { TooltipProvider } from "@/components/shadcn/tooltip";
import { useUserInputStore } from "@/stores";
import { useChat } from "@/hooks/useChat";

export default function App() {
  const setText = useUserInputStore((state) => state.setText);
  const appendText = useUserInputStore((state) => state.appendText);
  const clear = useUserInputStore((state) => state.clear);
  const { running, error, entries, send } = useChat();

  return (
    <TooltipProvider>
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset>
          <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4">
            <SidebarTrigger />
            <h1 className="text-sm font-semibold">Agent Demo</h1>
          </header>

          <main className="flex flex-1 flex-col items-center gap-4 py-8">
            <ChatMessages entries={entries} />
            {error && <p className="text-sm text-red-500">{error}</p>}

            <UserInput onSend={send} running={running} />

            <div className="flex flex-wrap justify-center gap-2">
              <Button onClick={() => setText("你好，这段内容由 store 写入。")}>
                写入一句话
              </Button>
              <Button variant="secondary" onClick={() => appendText("（追加）")}>
                末尾追加
              </Button>
              <Button variant="destructive" onClick={clear}>
                清空
              </Button>
            </div>
          </main>
        </SidebarInset>
      </SidebarProvider>
    </TooltipProvider>
  );
}
