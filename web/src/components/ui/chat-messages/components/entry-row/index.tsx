import { useEffect, useRef, useState } from "react";
import { CheckIcon, CopyIcon } from "lucide-react";
import type { ChatEntry } from "@/hooks/useChat";
import { copyTextForEntry } from "@/hooks/chatHistory";
import { Bubble, BubbleContent } from "@/components/shadcn/bubble";
import { Button } from "@/components/shadcn/button";
import { Message, MessageContent, MessageFooter } from "@/components/shadcn/message";
import { MessageScrollerItem } from "@/components/shadcn/message-scroller";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/shadcn/tooltip";
import AssistantEntry from "../assistant-entry";

const dateTimeFormat = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
});

/** 单条聊天记录的滚动行：负责条目包裹与轮次锚定（用户消息为锚点），正文按条目类型分流。 */
export default function EntryRow({ entry }: { entry: ChatEntry }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
  }, []);

  const copyText = copyTextForEntry(entry);
  const copyLabel = entry.kind === "user" ? "复制消息" : "复制回复";
  const tooltip = copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : copyLabel;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(copyText);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setCopyState("idle"), 2000);
  };
  const date = new Date(entry.createdAt * 1000);
  const footer = (
    <MessageFooter className="gap-1 px-0 text-muted-foreground">
      <time dateTime={date.toISOString()}>{dateTimeFormat.format(date)}</time>
      {copyText && (
        <Tooltip>
          <TooltipTrigger
            render={
              <Button type="button" variant="ghost" size="icon-xs" aria-label={copyLabel} onClick={() => { void copy(); }}>
                {copyState === "copied" ? <CheckIcon /> : <CopyIcon />}
              </Button>
            }
          />
          <TooltipContent>{tooltip}</TooltipContent>
        </Tooltip>
      )}
    </MessageFooter>
  );

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
            {footer}
          </MessageContent>
        </Message>
      ) : (
        <AssistantEntry events={entry.events} footer={footer} />
      )}
    </MessageScrollerItem>
  );
}
