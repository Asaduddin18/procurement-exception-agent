import type { NextRequest } from "next/server";

type Context = { params: Promise<{ path: string[] }> };
type StreamingRequest = RequestInit & { duplex?: "half" };

const uuid = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;
const base = process.env.PROCUREMENT_API_URL ?? "http://127.0.0.1:8000";

function allowed(method: string, [root, run, operation, ...extra]: string[]): boolean {
  if (extra.length) return false;
  if (method === "GET") {
    return root === "runs" && (run === undefined || (uuid.test(run) && (!operation || operation === "audit")));
  }
  return (root === "ingest" && run === undefined)
    || (root === "runs" && uuid.test(run ?? "") && (operation === "resolve" || operation === "test-duplicate"));
}

async function proxy(request: NextRequest, context: Context) {
  const { path } = await context.params;
  if (!allowed(request.method, path)) return Response.json({ detail: "Route not found." }, { status: 404 });

  const origin = request.headers.get("origin");
  if (request.method === "POST" && (
    request.headers.get("sec-fetch-site") === "cross-site" || (origin && origin !== request.nextUrl.origin)
  )) {
    return Response.json({ detail: "Use the Control Tower origin." }, { status: 403 });
  }

  const headers = new Headers();
  for (const name of ["content-type", "content-length"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (path[2] === "resolve") {
    const authorization = request.headers.get("authorization");
    if (authorization) headers.set("authorization", authorization);
  }
  try {
    const target = new URL("/api/v1/" + path.join("/"), base);
    if (target.protocol !== "http:" && target.protocol !== "https:") throw new Error("Invalid backend URL.");
    const limit = request.nextUrl.searchParams.get("limit");
    if (limit && path.length === 1 && path[0] === "runs") target.searchParams.set("limit", limit);
    const options: StreamingRequest = {
      method: request.method,
      headers,
      body: request.body,
      duplex: "half",
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(12000)]),
    };
    const result = await fetch(target, options);
    return new Response(result.body, {
      status: result.status,
      headers: {
        "Content-Type": result.headers.get("content-type") ?? "application/json",
        "Cache-Control": "no-store",
      },
    });
  } catch {
    return Response.json({ detail: "Procurement API is unavailable." }, { status: 502 });
  }
}

export { proxy as GET, proxy as POST };
