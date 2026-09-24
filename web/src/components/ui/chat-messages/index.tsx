import { Fragment } from "react";
import type { ChatMessage } from "@/chat/message";
import { compressionBoundaryIndex } from "@/chat/messageHistory";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/shadcn/message-scroller";
import MessageItem from "./components/message-item";
import CompressionDivider from "./components/compression-divider";

interface ChatMessagesProps {
  messages: ChatMessage[];
  summaryCursor: number | null;
}

/** 聊天记录列表：原文照常展示，游标仅决定分隔线的位置。 */
export default function ChatMessages({ messages, summaryCursor }: ChatMessagesProps) {
  const boundary = compressionBoundaryIndex(messages, summaryCursor);
  return (
    <MessageScrollerProvider autoScroll>
      <MessageScroller className="w-full flex-1">
        <MessageScrollerViewport className="scrollbar-thumb-border">
          <MessageScrollerContent className="mx-auto w-full max-w-[500px]">
            {messages.map((message, index) => (
              <Fragment key={message.id}>
                <MessageItem message={message} />
                {index === boundary && <CompressionDivider />}
              </Fragment>
            ))}
          </MessageScrollerContent>
        </MessageScrollerViewport>
        <MessageScrollerButton />
      </MessageScroller>
    </MessageScrollerProvider>
  );
}
