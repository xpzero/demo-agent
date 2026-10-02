import type { AgentEvent } from "@/adapter";
import { Marker, MarkerContent, MarkerIcon } from "@/components/shadcn/marker";
import { Spinner } from "@/components/shadcn/spinner";
import { deriveSegments, isAwaitingReply } from "../../utils/segments";
import ToolCallCard from "../tool-call-card";

/** 终态兜底文案：历史直读时 finish_kind 的可见呈现。 */
const FINISH_LABELS: Record<string, string> = {
  stopped: "已停止输出",
  interrupted: "生成中断，内容未完成",
  error: "生成出错",
  max_turns: "已达到最大轮次",
};

/** 从助手事件流派生正文分段及工具执行后的等待状态。 */
export default function AssistantMessageBody({
  events, completed, finishKind,
}: { events: AgentEvent[]; completed: boolean; finishKind?: string }) {
  const segments = deriveSegments(events);
  const finishLabel = FINISH_LABELS[finishKind ?? ""];
  const hasTerminalEvent = events.some(
    (event) => event.type === "done" || event.type === "error" || event.type === "stopped" || event.type === "max_turns",
  );
  return (
    <>
      {segments.map((segment, index) => {
        if (segment.kind === "text") {
          return (
            <p key={index} className="text-sm leading-relaxed whitespace-pre-wrap">
              {segment.text}
            </p>
          );
        }
        if (segment.kind === "note") {
          return (
            <Marker key={index} className="text-xs">
              <MarkerContent>{segment.text}</MarkerContent>
            </Marker>
          );
        }
        if (segment.kind === "tool") {
          return <ToolCallCard key={index} segment={segment} />;
        }
      })}
      {finishLabel && !hasTerminalEvent && (
        <Marker className="text-xs">
          <MarkerContent>{finishLabel}</MarkerContent>
        </Marker>
      )}
      {isAwaitingReply(events, completed) && (
        <Marker>
          <MarkerIcon><Spinner /></MarkerIcon>
          <MarkerContent>思考中…</MarkerContent>
        </Marker>
      )}
    </>
  );
}
