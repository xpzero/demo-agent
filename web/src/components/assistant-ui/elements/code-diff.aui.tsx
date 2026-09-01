import { FileCode2 } from "lucide-react";

import { cn } from "@/lib/utils";

export type DiffLine = {
  kind: "context" | "added" | "removed";
  text: string;
};

type CodeDiffProps = {
  filename: string;
  additions: number;
  deletions: number;
  lines: readonly DiffLine[];
  className?: string;
};

const lineStyle = {
  context: "text-stone-600 dark:text-stone-300",
  added: "bg-emerald-500/10 text-emerald-800 dark:text-emerald-300",
  removed: "bg-rose-500/10 text-rose-800 dark:text-rose-300",
};

const marker = { context: " ", added: "+", removed: "-" };

export function CodeDiff({
  filename,
  additions,
  deletions,
  lines,
  className,
}: CodeDiffProps) {
  return (
    <section
      className={cn(
        "overflow-hidden rounded-xl border border-stone-200 bg-white text-sm shadow-sm dark:border-stone-700 dark:bg-stone-950",
        className,
      )}
      data-slot="code-diff"
    >
      <header className="flex items-center gap-2 border-b border-stone-200 px-3 py-2 dark:border-stone-700">
        <FileCode2 className="size-4 text-stone-500" aria-hidden="true" />
        <code className="min-w-0 flex-1 truncate text-xs font-medium">{filename}</code>
        <span className="font-mono text-xs text-emerald-700 dark:text-emerald-400">
          +{additions}
        </span>
        <span className="font-mono text-xs text-rose-700 dark:text-rose-400">
          -{deletions}
        </span>
      </header>
      <div className="max-h-72 overflow-auto bg-stone-50 py-1 font-mono text-xs leading-5 dark:bg-stone-900">
        {lines.length === 0 ? (
          <p className="m-0 px-3 py-4 text-center text-stone-500">内容没有变化</p>
        ) : (
          lines.map((line, index) => (
            <div
              className={cn("flex min-w-max px-3", lineStyle[line.kind])}
              key={`${index}-${line.kind}-${line.text}`}
            >
              <span className="mr-3 select-none opacity-70">{marker[line.kind]}</span>
              <span className="whitespace-pre">{line.text || " "}</span>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
