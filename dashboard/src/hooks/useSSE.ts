import { useEffect, useRef } from "react";
import type { SessionData } from "../types";

export interface SSEHandlers {
  onLiveUpdate?: (prefix: string, session: SessionData) => void;
  onLiveStart?: (prefix: string) => void;
  onLiveEnd?: (prefix: string) => void;
  onSessionSaved?: (prefix: string, file: string) => void;
}

export function useSSE(handlers: SSEHandlers) {
  const ref = useRef(handlers);
  ref.current = handlers;

  useEffect(() => {
    let es: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let mounted = true;

    function connect() {
      es = new EventSource("/events");

      es.addEventListener("live-update", (e) => {
        const { prefix, session } = JSON.parse(
          (e as MessageEvent).data
        ) as { prefix: string; session: SessionData };
        ref.current.onLiveUpdate?.(prefix, session);
      });

      es.addEventListener("live-start", (e) => {
        const { prefix } = JSON.parse((e as MessageEvent).data) as {
          prefix: string;
        };
        ref.current.onLiveStart?.(prefix);
      });

      es.addEventListener("live-end", (e) => {
        const { prefix } = JSON.parse((e as MessageEvent).data) as {
          prefix: string;
        };
        ref.current.onLiveEnd?.(prefix);
      });

      es.addEventListener("session-saved", (e) => {
        const { prefix, file } = JSON.parse(
          (e as MessageEvent).data
        ) as { prefix: string; file: string };
        ref.current.onSessionSaved?.(prefix, file);
      });

      es.onerror = () => {
        es?.close();
        if (mounted) {
          timer = setTimeout(connect, 3000);
        }
      };
    }

    connect();

    return () => {
      mounted = false;
      es?.close();
      if (timer) clearTimeout(timer);
    };
  }, []);
}
