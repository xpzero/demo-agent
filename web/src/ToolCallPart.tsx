import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { useState } from "react";

import {
  ToolFallbackApproval,
  ToolFallbackArgs,
  ToolFallbackContent,
  ToolFallbackError,
  ToolFallbackResult,
  ToolFallbackRoot,
  ToolFallbackTrigger,
} from "@/components/assistant-ui/elements/tool-fallback.aui";
import {
  CodeDiff,
  type DiffLine,
} from "@/components/assistant-ui/elements/code-diff.aui";
import { submitToolDecision, type CodeDiffPreview } from "./adapter";
import { expandPreviewTools } from "./preview";

const isCodeDiff = (value: unknown): value is CodeDiffPreview => {
  if (!value || typeof value !== "object") return false;
  const preview = value as Partial<CodeDiffPreview>;
  return (
    preview.type === "code_diff" &&
    typeof preview.path === "string" &&
    typeof preview.additions === "number" &&
    typeof preview.deletions === "number" &&
    Array.isArray(preview.lines)
  );
};

export function ToolCallPart({
  toolCallId,
  toolName,
  argsText,
  result,
  artifact,
  status,
  addResult,
  resume,
  interrupt,
  approval,
  respondToApproval,
}: ToolCallMessagePartProps) {
  const isCancelled =
    status.type === "incomplete" && status.reason === "cancelled";
  const requiresAction = status.type === "requires-action";
  const [open, setOpen] = useState(expandPreviewTools || requiresAction);
  const [wasRequiresAction, setWasRequiresAction] = useState(requiresAction);
  const [submitting, setSubmitting] = useState(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [decisionOutput, setDecisionOutput] = useState<string | null>(null);
  const diff = isCodeDiff(artifact) ? artifact : null;

  const answerApproval = async (approved: boolean) => {
    if (!respondToApproval) return;
    setSubmitting(true);
    setDecisionError(null);
    try {
      const decision = await submitToolDecision(toolCallId, approved);
      setDecisionOutput(decision.output);
      respondToApproval({
        approved,
        ...(!approved && { reason: `用户拒绝执行工具 ${toolName}` }),
      });
    } catch (error) {
      setDecisionError(error instanceof Error ? error.message : String(error));
    } finally {
      setSubmitting(false);
    }
  };

  if (requiresAction !== wasRequiresAction) {
    setWasRequiresAction(requiresAction);
    if (requiresAction) setOpen(true);
  }

  return (
    <ToolFallbackRoot open={open} onOpenChange={setOpen}>
      <ToolFallbackTrigger toolName={toolName} status={status} />
      <ToolFallbackContent>
        <ToolFallbackError status={status} />
        {diff ? (
          <CodeDiff
            filename={diff.path}
            additions={diff.additions}
            deletions={diff.deletions}
            lines={diff.lines as DiffLine[]}
            className={isCancelled ? "opacity-60" : undefined}
          />
        ) : (
          <ToolFallbackArgs
            argsText={argsText}
            className={isCancelled ? "opacity-60" : undefined}
          />
        )}
        {requiresAction && approval?.approved === undefined && approval && (
          <div className="flex flex-col gap-2 pt-2">
            <p className="m-0 text-xs text-stone-600 dark:text-stone-300">
              {diff
                ? "文件尚未修改。请检查上面的拟议改动。"
                : "这个工具需要确认后才能执行。"}
            </p>
            {decisionError && (
              <p className="m-0 text-xs text-rose-700 dark:text-rose-300">
                {decisionError}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <button
                type="button"
                className="rounded-lg border border-stone-300 px-3 py-1.5 text-xs font-medium hover:bg-stone-100 disabled:cursor-wait disabled:opacity-50 dark:border-stone-600 dark:hover:bg-stone-800"
                disabled={submitting}
                onClick={() => void answerApproval(false)}
              >
                拒绝
              </button>
              <button
                type="button"
                className="rounded-lg bg-stone-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-stone-700 disabled:cursor-wait disabled:opacity-50 dark:bg-stone-100 dark:text-stone-900"
                disabled={submitting}
                onClick={() => void answerApproval(true)}
              >
                {submitting
                  ? "提交中…"
                  : diff
                    ? "批准并写入"
                    : "批准并执行"}
              </button>
            </div>
          </div>
        )}
        {requiresAction && !approval && (
          <ToolFallbackApproval
            addResult={addResult}
            resume={resume}
            interrupt={interrupt}
            approval={approval}
            respondToApproval={respondToApproval}
            status={status}
          />
        )}
        {!isCancelled && (
          <ToolFallbackResult result={decisionOutput ?? result} />
        )}
      </ToolFallbackContent>
    </ToolFallbackRoot>
  );
}
