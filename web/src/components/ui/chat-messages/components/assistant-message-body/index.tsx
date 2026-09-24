import type { AgentEvent } from "@/adapter";
import { Marker, MarkerContent, MarkerIcon } from "@/components/shadcn/marker";
import { Spinner } from "@/components/shadcn/spinner";
import { deriveSegments, isAwaitingReply } from "../../utils/segments";
import ToolCallCard from "../tool-call-card";

/** 从助手事件流派生正文分段及工具执行后的等待状态。 */
export default function AssistantMessageBody({ events, completed }: { events: AgentEvent[]; completed: boolean }) {
  const segments = deriveSegments(events);
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
      {isAwaitingReply(events, completed) && (
        <Marker>
          <MarkerIcon><Spinner /></MarkerIcon>
          <MarkerContent>思考中…</MarkerContent>
        </Marker>
      )}
    </>
  );
}
