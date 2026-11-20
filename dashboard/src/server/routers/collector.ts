import { z } from "zod";
import * as fs from "fs";
import * as path from "path";
import { spawn } from "child_process";
import { router, procedure } from "../trpc";
import { PROJECT_ROOT } from "../helpers";
import { safeSlugKey } from "../controlGuard";

const restartState: Record<string, { status: string; old_pid: number | null; new_pid: number | null; error?: string }> = {};

export const collectorRouter = router({
  start: procedure
    .input(z.object({
      slug_prefix: z.string(),
      window: z.number().optional(),
    }))
    .mutation(async ({ input }) => {
      const slugPrefix = input.slug_prefix.trim();
      const slugKey = safeSlugKey(slugPrefix);
      if (!slugKey) return { ok: false, error: "invalid slug_prefix" };
      const pidFile = `/tmp/fondaco-${slugKey}-collector.pid`;

      if (fs.existsSync(pidFile)) {
        try {
          const pid = parseInt(fs.readFileSync(pidFile, "utf-8").trim(), 10);
          process.kill(pid, 0);
          return { ok: true, already_running: true, pid };
        } catch {}
      }

      const args = ["--slug-prefix", slugPrefix];
      if (input.window) args.push("--window", String(input.window));

      const bin = path.join(PROJECT_ROOT, "target", "release", "collector");
      let spawnErr: string | null = null;
      const child = spawn(bin, args, {
        cwd: PROJECT_ROOT,
        detached: true,
        stdio: "ignore",
      });
      child.on("error", (err) => { spawnErr = err.message; });
      child.unref();

      await new Promise((resolve) => setTimeout(resolve, 1500));

      if (spawnErr) {
        return { ok: false, error: `collector spawn failed: ${spawnErr}` };
      }
      return { ok: true, pid: child.pid };
    }),

  stop: procedure
    .input(z.object({
      slug_prefix: z.string(),
      after_session: z.boolean().optional(),
    }))
    .mutation(({ input }) => {
      const slugKey = safeSlugKey(input.slug_prefix);
      if (!slugKey) return { ok: false, error: "invalid slug_prefix" };
      const pidFile = `/tmp/fondaco-${slugKey}-collector.pid`;

      if (!fs.existsSync(pidFile)) {
        return { ok: false, error: "No collector PID file found" };
      }

      const pid = parseInt(fs.readFileSync(pidFile, "utf-8").trim(), 10);
      try {
        process.kill(pid, 0);
      } catch {
        return { ok: false, error: "Collector not running (stale PID)" };
      }

      const sig = input.after_session ? "SIGUSR1" : "SIGTERM";
      process.kill(pid, sig);

      return { ok: true, signal: sig, pid };
    }),

  restart: procedure
    .input(z.object({
      slug_prefix: z.string(),
      rebuild: z.boolean().optional(),
    }))
    .mutation(({ input }) => {
      const slugPrefix = input.slug_prefix.trim();
      const slugKey = safeSlugKey(slugPrefix);
      if (!slugKey) return { ok: false, error: "invalid slug_prefix" };
      const pidFile = `/tmp/fondaco-${slugKey}-collector.pid`;
      const bin = path.join(PROJECT_ROOT, "target", "release", "collector");

      if (restartState[slugKey]?.status === "waiting" || restartState[slugKey]?.status === "rebuilding") {
        return { ok: true, already_pending: true, ...restartState[slugKey] };
      }

      let oldPid: number | null = null;
      if (fs.existsSync(pidFile)) {
        oldPid = parseInt(fs.readFileSync(pidFile, "utf-8").trim(), 10);
        try {
          process.kill(oldPid, 0);
          process.kill(oldPid, "SIGUSR1");
        } catch {
          oldPid = null;
        }
      }

      restartState[slugKey] = { status: oldPid ? "waiting" : "starting", old_pid: oldPid, new_pid: null };
      const result = { ok: true, status: restartState[slugKey].status, old_pid: oldPid };

      // Background restart
      (async () => {
        try {
          if (input.rebuild) {
            restartState[slugKey].status = "rebuilding";
            const cargoPath = path.join(process.env.CARGO_HOME || path.join(process.env.HOME || "", ".cargo"), "bin", "cargo");
            const build = spawn(cargoPath, ["build", "--release"], {
              cwd: PROJECT_ROOT,
              stdio: "pipe",
            });
            let buildStderr = "";
            build.stderr?.on("data", (d: Buffer) => { buildStderr += d.toString(); });
            const buildResult = await new Promise<number | null>((resolve) => {
              build.on("close", resolve);
              build.on("error", (err) => { buildStderr = err.message; resolve(null); });
            });
            if (buildResult !== 0) {
              const lastLines = buildStderr.trim().split("\n").slice(-5).join("\n");
              restartState[slugKey] = { status: "error", old_pid: oldPid, new_pid: null, error: `cargo build failed: ${lastLines || "unknown error"}` };
              return;
            }
          }

          let spawnError: string | null = null;
          const child = spawn(bin, ["--slug-prefix", slugPrefix], {
            cwd: PROJECT_ROOT,
            detached: true,
            stdio: "ignore",
          });
          child.on("error", (err) => { spawnError = err.message; });
          child.unref();
          await new Promise((resolve) => setTimeout(resolve, 1500));

          if (spawnError) {
            restartState[slugKey] = { status: "error", old_pid: oldPid, new_pid: null, error: `collector spawn failed: ${spawnError}` };
          } else {
            restartState[slugKey] = { status: "done", old_pid: oldPid, new_pid: child.pid ?? null };
          }
        } catch (err) {
          restartState[slugKey] = { status: "error", old_pid: oldPid, new_pid: null, error: String(err) };
        }
      })();

      return result;
    }),

  restartStatus: procedure
    .input(z.object({ slug_prefix: z.string().optional() }))
    .query(({ input }) => {
      const raw = input.slug_prefix || "";
      if (!raw) return { restarts: restartState };
      const slugKey = safeSlugKey(raw);
      if (!slugKey) return { status: "none" };
      return restartState[slugKey] || { status: "none" };
    }),

  toggleBookSource: procedure
    .input(z.object({ slug_prefix: z.string() }))
    .mutation(({ input }) => {
      const slugKey = safeSlugKey(input.slug_prefix);
      if (!slugKey) return { ok: false, error: "invalid slug_prefix" };
      const pidFile = `/tmp/fondaco-${slugKey}-collector.pid`;

      if (!fs.existsSync(pidFile)) {
        return { ok: false, error: "No collector PID file found" };
      }

      const pid = parseInt(fs.readFileSync(pidFile, "utf-8").trim(), 10);
      try {
        process.kill(pid, 0);
      } catch {
        return { ok: false, error: "Collector not running (stale PID)" };
      }

      process.kill(pid, "SIGUSR2");
      return { ok: true, pid };
    }),

  bookSource: procedure
    .input(z.object({ slug_prefix: z.string() }))
    .query(({ input }) => {
      const slugKey = safeSlugKey(input.slug_prefix);
      if (!slugKey) return { book_source: "rest" };
      const statePath = path.join(PROJECT_ROOT, "data", "markets", slugKey, "collector_state.json");
      try {
        const data = JSON.parse(fs.readFileSync(statePath, "utf-8"));
        return { book_source: data.book_source || "rest" };
      } catch {
        return { book_source: "rest" };
      }
    }),
});
