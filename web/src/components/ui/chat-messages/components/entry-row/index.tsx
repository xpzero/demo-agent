import type { ChatEntry } from "@/hooks/useChat";
import { Bubble, BubbleContent } from "@/components/shadcn/bubble";
import { Message, MessageContent } from "@/components/shadcn/message";
import { MessageScrollerItem } from "@/components/shadcn/message-scroller";
import AssistantEntry from "../assistant-entry";

/** 单条聊天记录的滚动行：负责条目包裹与轮次锚定（用户消息为锚点），正文按条目类型分流。 */
export default function EntryRow({ entry }: { entry: ChatEntry }) {
  return (
    <MessageScrollerItem messageId={entry.id} scrollAnchor={entry.kind === "user"}>
      {entry.kind === "user" ? (
        <Message align="end">
          <MessageContent>
            <Bubble align="end">
              <BubbleContent className="whitespace-pre-wrap">
                {entry.text}
              </BubbleContent>
            </Bubble>
          </MessageContent>
        </Message>
      ) : (
        <AssistantEntry events={entry.events} />
      )}
    </MessageScrollerItem>
  );
}
