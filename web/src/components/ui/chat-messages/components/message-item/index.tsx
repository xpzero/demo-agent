import type { ChatMessage } from "@/chat/message";
import { Bubble, BubbleContent } from "@/components/shadcn/bubble";
import { Message, MessageContent } from "@/components/shadcn/message";
import { MessageScrollerItem } from "@/components/shadcn/message-scroller";
import AssistantMessageBody from "../assistant-message-body";
import MessageMeta from "../message-meta";

/** 单条消息的对齐方式、正文和操作。 */
export default function MessageItem({ message }: { message: ChatMessage }) {
  return (
    <MessageScrollerItem className="group/message" messageId={message.id}>
      <Message align={message.kind === "user" ? "end" : "start"}>
        <MessageContent>
          {message.kind === "user" ? (
            <Bubble align="end">
              <BubbleContent className="whitespace-pre-wrap">
                {message.text}
              </BubbleContent>
            </Bubble>
          ) : (
            <AssistantMessageBody events={message.events} completed={message.messageId !== undefined} />
          )}
          <MessageMeta message={message} />
        </MessageContent>
      </Message>
    </MessageScrollerItem>
  );
}
