import type { AgentEvent } from "@/adapter";
import { Marker, MarkerContent, MarkerIcon } from "@/components/shadcn/marker";
import { Spinner } from "@/components/shadcn/spinner";
import { CircleCheckIcon } from "lucide-react";
import { deriveSegments } from "../../utils/segments";

/** 从助手事件流派生正文分段；没有可见分段时显示「思考中」。 */
export default function AssistantMessageBody({ events }: { events: AgentEvent[] }) {
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
      {segments.length === 0 && (
        <Marker>
          <MarkerIcon>
            <Spinner />
          </MarkerIcon>
          <MarkerContent>思考中…</MarkerContent>
        </Marker>
      )}
    </>
  );
}
