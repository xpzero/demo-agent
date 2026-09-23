import type { ChatMessage } from "@/chat/message";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/shadcn/message-scroller";
import MessageItem from "./components/message-item";

interface ChatMessagesProps {
  messages: ChatMessage[];
}

/** 聊天记录列表：条目顺序渲染；autoScroll 仅在读者位于底部时跟随流式输出。 */
export default function ChatMessages({ messages }: ChatMessagesProps) {
  return (
    <MessageScrollerProvider autoScroll>
      <MessageScroller className="w-full flex-1">
        <MessageScrollerViewport className="scrollbar-thumb-border">
          <MessageScrollerContent className="mx-auto w-full max-w-[500px]">
            {messages.map((message) => (
              <MessageItem key={message.id} message={message} />
            ))}
          </MessageScrollerContent>
        </MessageScrollerViewport>
        <MessageScrollerButton />
      </MessageScroller>
    </MessageScrollerProvider>
  );
}
