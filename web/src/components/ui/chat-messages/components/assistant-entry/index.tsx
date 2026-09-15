import type { AgentEvent } from "@/adapter";
import { Marker, MarkerContent, MarkerIcon } from "@/components/shadcn/marker";
import { Message, MessageContent } from "@/components/shadcn/message";
import { Spinner } from "@/components/shadcn/spinner";
import { CircleCheckIcon } from "lucide-react";
import { deriveSegments } from "../../utils/segments";

/** 助手条目：从原始事件派生分段渲染，事件为空时显示「思考中」。 */
export default function AssistantEntry({ events }: { events: AgentEvent[] }) {
  const segments = deriveSegments(events);
  return (
    <Message align="start">
      <MessageContent>
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
            return (
              <Marker key={index}>
                <MarkerIcon>
                  {segment.output === null ? <Spinner /> : <CircleCheckIcon />}
                </MarkerIcon>
                <MarkerContent>{segment.name}</MarkerContent>
              </Marker>
            );
          }
        })}
        {events.length === 0 && (
          <Marker>
            <MarkerIcon>
              <Spinner />
            </MarkerIcon>
            <MarkerContent>思考中…</MarkerContent>
          </Marker>
        )}
      </MessageContent>
    </Message>
  );
}
