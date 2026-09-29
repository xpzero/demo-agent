import { getSessionHistory, getSessionStatus } from "../adapter/index.ts";

/** 默认 5 秒是前端实现选择；可按服务端负载配置在 2–60 秒之间。 */
export function recoveryPollInterval(value?: string): number {
  const interval = Number(value);
  if (!Number.isFinite(interval) || interval <= 0) {
    return 5000;
  }
  return Math.min(60000, Math.max(2000, interval));
}

/** 只有状态与历史都允许发送，才完成恢复；历史再次检查期间可能出现的新请求。 */
export async function recoverSession(sessionId: string, signal: AbortSignal) {
  const status = await getSessionStatus(sessionId, signal);
  if (status && !status.can_send_message) {
    return { ready: false as const };
  }
  const history = await getSessionHistory(sessionId, signal);
  if (history && !history.can_send_message) {
    return { ready: false as const };
  }
  return { ready: true as const, history };
}
