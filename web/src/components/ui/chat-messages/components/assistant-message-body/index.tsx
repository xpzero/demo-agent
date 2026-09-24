import { useState } from "react";
import type { AgentEvent } from "@/adapter";
import { Marker, MarkerContent, MarkerIcon } from "@/components/shadcn/marker";
import { Spinner } from "@/components/shadcn/spinner";
import { CircleCheckIcon } from "lucide-react";
import { deriveSegments } from "../../utils/segments";

/** 毫秒 → 人类可读：小于 1s 显示 ms，否则一位小数秒。 */
function formatElapsed(ms: number): string {
  if (ms < 1000) {
    return `${Math.round(ms)}ms`;
  }
  return `${(ms / 1000).toFixed(1)}s`;
}

/** 单个工具卡片：收起显示名称与耗时，点击展开参数与结果。 */
function ToolCallCard({ segment }: { segment: ReturnType<typeof deriveSegments>[number] & { kind: "tool" } }) {
  const [expanded, setExpanded] = useState(false);
  const running = segment.output === null;
  return (
    <div className="w-full">
      <button
        type="button"
        className="flex w-full items-center gap-1 text-left"
        onClick={() => {
          if (!running) {
            setExpanded((value) => !value);
          }
        }}
        disabled={running}
      >
        <Marker>
          <MarkerIcon>
            {running ? <Spinner /> : <CircleCheckIcon />}
          </MarkerIcon>
          <MarkerContent>
            {segment.name}
            {!running && segment.elapsed !== null && (
              <span className="ml-1 text-xs text-muted-foreground/70">
                · {formatElapsed(segment.elapsed)}
              </span>
            )}
          </MarkerContent>
        </Marker>
      </button>
      {expanded && (
        <div className="mt-1 space-y-1 rounded-md border bg-muted/40 p-2 text-xs">
          <div>
            <span className="font-medium">参数</span>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-all">
              {JSON.stringify(segment.args ?? {}, null, 2)}
            </pre>
          </div>
          <div>
            <span className="font-medium">结果</span>
            <pre className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap break-all">
              {segment.output}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}

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
          return <ToolCallCard key={index} segment={segment} />;
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
