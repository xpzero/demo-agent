import { useState } from "react";
import { CircleCheckIcon } from "lucide-react";
import { Marker, MarkerContent, MarkerIcon } from "@/components/shadcn/marker";
import { Spinner } from "@/components/shadcn/spinner";
import type { ToolSegment } from "../../utils/segments";

function formatElapsed(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** 实时显示完整参数和结果；历史回放使用 tool_runs 的短文本。 */
export default function ToolCallCard({ segment }: { segment: ToolSegment }) {
  const [expanded, setExpanded] = useState(false);
  const running = segment.output === null;
  return (
    <div className="w-full">
      <button
        type="button"
        className="flex w-full items-center gap-1 text-left"
        onClick={() => setExpanded((value) => !value)}
        disabled={running}
        aria-expanded={running ? undefined : expanded}
      >
        <Marker>
          <MarkerIcon>{running ? <Spinner /> : <CircleCheckIcon />}</MarkerIcon>
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
          {segment.excerpt && <p className="text-muted-foreground">仅展示存档短文本（最多 500 字，可能已截断）</p>}
          <div>
            <span className="font-medium">参数</span>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-all">
              {typeof segment.args === "string" ? segment.args : JSON.stringify(segment.args ?? {}, null, 2)}
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
