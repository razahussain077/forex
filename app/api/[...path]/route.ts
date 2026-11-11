import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };

function workerBase(): URL {
  const raw = process.env.WORKER_API_URL || "";
  if (!raw) throw new Error("WORKER_API_URL is not configured");
  const url = new URL(raw);
  if (url.protocol !== "https:" && process.env.NODE_ENV === "production") {
    throw new Error("WORKER_API_URL must use HTTPS in production");
  }
  return url;
}

async function forward(request: NextRequest, context: Context) {
  const { path } = await context.params;
  const safePath = path.map(part => decodeURIComponent(part));
  // Public dashboard is strictly read-only. Tenant calendar/journal and all
  // POST routes stay private until a real per-user authentication flow exists.
  const allowed = request.method === "GET" && (
    (safePath.length === 1 && ["status", "universe", "signals", "correlation"].includes(safePath[0])) ||
    (safePath.length === 2 && ["market", "institutional", "backtest"].includes(safePath[0]) && /^[A-Za-z0-9._+-]{2,32}$/.test(safePath[1]))
  );
  if (!allowed) return NextResponse.json({ error: "Route not found" }, { status: 404 });

  const token = process.env.DASHBOARD_API_TOKEN || "";
  if (token.length < 32) {
    return NextResponse.json({ error: "Dashboard data connection is not configured" }, { status: 503 });
  }

  try {
    const base = workerBase();
    const target = new URL("/api/" + safePath.map(encodeURIComponent).join("/"), base);
    target.search = request.nextUrl.search;
    const headers = new Headers({ "X-Dashboard-Token": token, "Accept": "application/json" });
    const upstream = await fetch(target, {
      method: "GET",
      headers,
      cache: "no-store",
      signal: AbortSignal.timeout(15000),
    });
    const responseBody = await upstream.text();
    return new NextResponse(responseBody, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") || "application/json",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch {
    return NextResponse.json({ error: "Market data service is unavailable" }, { status: 503 });
  }
}

export const GET = forward;
export const POST = forward;

