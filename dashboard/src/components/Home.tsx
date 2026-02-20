import type { PrefixInfo } from "../types";

interface Props {
  prefixes: PrefixInfo[];
}

export function Home({ prefixes }: Props) {
  const total = prefixes.reduce((sum, p) => sum + p.count, 0);

  return (
    <div>
      <h2>Dashboard</h2>
      <p className="subtitle">
        Select a session from the sidebar, or watch live data as it streams in.
      </p>
      <div className="stats-grid">
        <div className="stat-card">
          <div className="label">Total Sessions</div>
          <div className="value">{total}</div>
        </div>
        <div className="stat-card">
          <div className="label">Prefixes</div>
          <div className="value">{prefixes.length}</div>
        </div>
        {prefixes.map((p) => (
          <div key={p.name} className="stat-card">
            <div className="label">{p.name}</div>
            <div className="value">{p.count}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
