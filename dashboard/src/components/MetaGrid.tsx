import type { SessionData } from "../types";
import { fmtTime } from "../utils/formatTime";

interface Props {
  session: SessionData;
}

function fmtPrice(v: number | null): string {
  if (v == null) return "\u2014";
  return "$" + v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export function MetaGrid({ session }: Props) {
  const meta = session.metadata;

  let btcChange: number | null = null;
  let btcClass = "";
  let btcIsLive = false;
  if (session.initial_btc_price_usd != null) {
    let currentBtc = session.final_btc_price_usd;
    if (currentBtc == null && session.samples?.length) {
      for (let i = session.samples.length - 1; i >= 0; i--) {
        if (session.samples[i].btc_price_usd != null) {
          currentBtc = session.samples[i].btc_price_usd;
          btcIsLive = true;
          break;
        }
      }
    }
    if (currentBtc != null) {
      btcChange = currentBtc - session.initial_btc_price_usd;
      btcClass = btcChange > 0 ? "up" : btcChange < 0 ? "down" : "";
    }
  }

  return (
    <div className="meta-grid">
      <div className="meta-card">
        <div className="meta-label">Outcome</div>
        <div className={`meta-value ${(session.outcome || "").toLowerCase()}`}>
          {session.outcome || "Pending"}
        </div>
      </div>
      <div className="meta-card">
        <div className="meta-label">Window</div>
        <div className="meta-value" style={{ fontSize: 14 }}>
          {fmtTime(meta?.window_start_utc)}
        </div>
      </div>
      <div className="meta-card">
        <div className="meta-label">Initial BTC</div>
        <div className="meta-value">{fmtPrice(session.initial_btc_price_usd)}</div>
      </div>
      <div className="meta-card">
        <div className="meta-label">{btcIsLive ? "BTC Now" : "BTC Change"}</div>
        <div className={`meta-value ${btcClass}`}>
          {btcChange !== null
            ? (btcChange > 0 ? "UP +" : btcChange < 0 ? "DOWN " : "") + btcChange.toFixed(2)
            : "\u2014"}
        </div>
      </div>
      <div className="meta-card">
        <div className="meta-label">Samples</div>
        <div className="meta-value">{session.samples?.length ?? 0}</div>
      </div>
      <div className="meta-card">
        <div className="meta-label">Errors</div>
        <div className="meta-value">{session.error_count ?? 0}</div>
      </div>
    </div>
  );
}
