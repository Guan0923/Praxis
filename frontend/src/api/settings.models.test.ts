import { createServer, type Server } from "node:http";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { discoverProviderModels } from "./settings";

const nativeFetch = globalThis.fetch;
let server: Server;
let calls: string[];
let failing: boolean;

beforeEach(async () => {
  calls = [];
  failing = false;
  server = createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => { body += String(chunk); });
    request.on("end", () => {
      const values = JSON.parse(body) as { config_id: string };
      calls.push(values.config_id);
      setTimeout(() => {
        response.writeHead(failing ? 502 : 200, { "Content-Type": "application/json" });
        response.end(JSON.stringify(failing ? { detail: "local provider unavailable" } : { models: [values.config_id] }));
      }, 20);
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("No local test port");
  vi.stubGlobal("fetch", (url: string, init: RequestInit) => nativeFetch(`http://127.0.0.1:${address.port}${url}`, init));
});

afterEach(async () => {
  vi.unstubAllGlobals();
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

const values = (id: string) => ({ config_id: id, provider_name: "local", protocol: "chat_completions" as const, base_url: "http://127.0.0.1/v1" });

describe("model discovery over real loopback HTTP", () => {
  it("shares identical in-flight queries and releases them after completion", async () => {
    const first = discoverProviderModels(values("one"));
    const second = discoverProviderModels(values("one"));
    expect(first).toBe(second);
    await expect(first).resolves.toEqual({ models: ["one"] });
    expect(calls).toEqual(["one"]);
    await discoverProviderModels(values("one"));
    expect(calls).toEqual(["one", "one"]);
  });

  it("keeps different query parameters separate", async () => {
    const results = await Promise.all([discoverProviderModels(values("one")), discoverProviderModels(values("two"))]);
    expect(results).toEqual([{ models: ["one"] }, { models: ["two"] }]);
    expect(calls.sort()).toEqual(["one", "two"]);
  });

  it("does not retain failed queries", async () => {
    failing = true;
    await expect(discoverProviderModels(values("one"))).rejects.toThrow("local provider unavailable");
    failing = false;
    await expect(discoverProviderModels(values("one"))).resolves.toEqual({ models: ["one"] });
    expect(calls).toHaveLength(2);
  });
});
