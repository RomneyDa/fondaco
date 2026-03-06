import { useEffect, useRef } from "react";
import type { BotStatus, LivePayload } from "../types";

export interface SSEHandlers {
  onLiveUpdate?: (prefix: string, data: LivePayload) => void;
  onLiveStart?: (prefix: string) => void;
  onLiveEnd?: (prefix: string) => void;
  onSessionSaved?: (prefix: string, file: string) => void;
  onBotStatus?: (status: BotStatus) => void;
}

export function useSSE(handlers: SSEHandlers) {
  const ref = useRef(handlers);
  ref.current = handlers;

  useEffect(() => {
    let es: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let mounted = true;

    function onLiveUpdate(e: Event) {
      const { prefix, session } = JSON.parse(
        (e as MessageEvent).data
      ) as { prefix: string; session: LivePayload };
      ref.current.onLiveUpdate?.(prefix, session);
    }
    function onLiveStart(e: Event) {
      const { prefix } = JSON.parse((e as MessageEvent).data) as {
        prefix: string;
      };
      ref.current.onLiveStart?.(prefix);
    }
    function onLiveEnd(e: Event) {
      const { prefix } = JSON.parse((e as MessageEvent).data) as {
        prefix: string;
      };
      ref.current.onLiveEnd?.(prefix);
    }
    function onSessionSaved(e: Event) {
      const { prefix, file } = JSON.parse(
        (e as MessageEvent).data
      ) as { prefix: string; file: string };
      ref.current.onSessionSaved?.(prefix, file);
    }
    function onBotStatus(e: Event) {
      const data = JSON.parse((e as MessageEvent).data) as BotStatus;
      ref.current.onBotStatus?.(data);
    }

    function connect() {
      es = new EventSource("/events");

      es.addEventListener("live-update", onLiveUpdate);
      es.addEventListener("live-start", onLiveStart);
      es.addEventListener("live-end", onLiveEnd);
      es.addEventListener("session-saved", onSessionSaved);
      es.addEventListener("bot-status", onBotStatus);

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
      if (timer) clearTimeout(timer);
      if (es) {
        es.removeEventListener("live-update", onLiveUpdate);
        es.removeEventListener("live-start", onLiveStart);
        es.removeEventListener("live-end", onLiveEnd);
        es.removeEventListener("session-saved", onSessionSaved);
        es.removeEventListener("bot-status", onBotStatus);
        es.close();
      }
    };
  }, []);
}
