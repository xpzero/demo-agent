import { ActivityIcon } from "lucide-react";
import type { SessionStats } from "@/adapter";
import { Button } from "@/components/shadcn/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/shadcn/tooltip";

/** 顶栏的会话观测入口：点开显示「本会话：N 轮请求 / X token」。 */
export default function SessionStatsBadge({ stats }: { stats: SessionStats | null }) {
  if (!stats) {
    return null;
  }
  const tokens = stats.estimated
    ? stats.estimated_prompt_tokens
    : stats.total_prompt_tokens + stats.total_completion_tokens;
  const suffix = stats.estimated ? "（估）" : "";
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="会话统计"
          >
            <ActivityIcon />
          </Button>
        }
      />
      <TooltipContent>
        <p>本会话：{stats.turns} 次模型请求</p>
        <p>
          约 {tokens.toLocaleString("zh-CN")} token{suffix}
        </p>
        {stats.estimated && <p>接口未返回实测用量，按字符估算</p>}
      </TooltipContent>
    </Tooltip>
  );
}
