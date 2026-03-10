import type { PrefixInfo } from "../types";

interface Props {
  prefixes: PrefixInfo[];
}

export function Home({ prefixes }: Props) {
  const total = prefixes.reduce((sum, p) => sum + p.count, 0);

  return (
    <div>
      <h2 className="text-xl font-semibold mb-2">Dashboard</h2>
      <p className="text-dim text-sm mb-6">
        Select a session from the sidebar, or watch live data as it streams in.
      </p>
      <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3">
        <div className="bg-card border border-border rounded-lg px-5 py-4">
          <div className="text-[13px] text-dim mb-1.5">Total Sessions</div>
          <div className="text-2xl font-semibold">{total}</div>
        </div>
        <div className="bg-card border border-border rounded-lg px-5 py-4">
          <div className="text-[13px] text-dim mb-1.5">Prefixes</div>
          <div className="text-2xl font-semibold">{prefixes.length}</div>
        </div>
        {prefixes.map((p) => (
          <div key={p.name} className="bg-card border border-border rounded-lg px-5 py-4">
            <div className="text-[13px] text-dim mb-1.5">{p.name}</div>
            <div className="text-2xl font-semibold">{p.count}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
