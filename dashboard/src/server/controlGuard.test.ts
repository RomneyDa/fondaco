import assert from "node:assert/strict";
import test from "node:test";
import { resolveListen, safeName, safeSlugKey, tokenAccepted } from "./controlGuard";

test("defaults to loopback and rejects a public bind without a token", () => {
  assert.deepEqual(resolveListen(undefined, undefined), { host: "127.0.0.1", token: "" });
  assert.throws(() => resolveListen("0.0.0.0", ""), /non-loopback/);
  assert.deepEqual(resolveListen("0.0.0.0", "s3cret"), { host: "0.0.0.0", token: "s3cret" });
});

test("market names are a single path component", () => {
  assert.equal(safeSlugKey("btc-updown-5m-"), "btc-updown-5m");
  assert.equal(safeName("PoissonKelly"), "PoissonKelly");
  assert.equal(safeSlugKey("../etc"), null);
  assert.equal(safeSlugKey("btc/up"), null);
  assert.equal(safeSlugKey(".."), null);
  assert.equal(safeName("a\\b"), null);
  assert.equal(safeSlugKey(""), null);
});

test("token compare", () => {
  assert.equal(tokenAccepted("", ""), true);
  assert.equal(tokenAccepted("abc", "abc"), true);
  assert.equal(tokenAccepted("abc", "abd"), false);
  assert.equal(tokenAccepted("abc", "abcd"), false);
});
