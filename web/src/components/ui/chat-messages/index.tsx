import type { ChatEntry } from "@/hooks/useChat";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/shadcn/message-scroller";
import EntryRow from "./components/entry-row";

interface ChatMessagesProps {
  entries: ChatEntry[];
}

/** 聊天记录列表：条目顺序渲染；autoScroll 仅在读者位于底部时跟随流式输出。 */
export default function ChatMessages({ entries }: ChatMessagesProps) {
  return (
    <MessageScrollerProvider autoScroll>
      <MessageScroller className="w-full flex-1">
        <MessageScrollerViewport className="scrollbar-thumb-border">
          <MessageScrollerContent className="mx-auto w-full max-w-[500px]">
            {entries.map((entry) => (
              <EntryRow key={entry.id} entry={entry} />
            ))}
          </MessageScrollerContent>
        </MessageScrollerViewport>
        <MessageScrollerButton />
      </MessageScroller>
    </MessageScrollerProvider>
  );
}
