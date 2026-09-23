import { useEffect, useRef, useState } from "react";
import { CheckIcon, CopyIcon } from "lucide-react";
import type { ChatMessage } from "@/chat/message";
import { getMessageText } from "@/chat/message";
import { Button } from "@/components/shadcn/button";
import { MessageFooter } from "@/components/shadcn/message";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/shadcn/tooltip";

const dateTimeFormat = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
});

export default function MessageMeta({ message }: { message: ChatMessage }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
  }, []);

  const text = getMessageText(message);
  const copyLabel = message.kind === "user" ? "复制消息" : "复制回复";
  const tooltip = copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : copyLabel;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setCopyState("idle"), 2000);
  };
  const date = new Date(message.createdAt * 1000);

  return (
    <MessageFooter className="gap-1 px-0 text-muted-foreground">
      <time dateTime={date.toISOString()}>{dateTimeFormat.format(date)}</time>
      {text && (
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
}
