import { useCallback, useEffect, useRef, useState } from "react";
import { listSessions, type SessionSummary } from "@/adapter";

export function useSessions() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const requestRef = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true);
    setError("");
    try {
      const items = await listSessions(controller.signal);
      if (requestRef.current === controller) setSessions(items);
    } catch (cause) {
      if (!controller.signal.aborted && requestRef.current === controller) {
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    } finally {
      if (requestRef.current === controller) {
        requestRef.current = null;
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
    return () => {
      requestRef.current?.abort();
      requestRef.current = null;
    };
  }, [refresh]);

  return { sessions, loading, error, refresh };
}
