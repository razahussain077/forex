import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Public read-only dashboard: no workspace password or session cookie is required.
export async function GET() {
  return NextResponse.json(
    { authenticated: true, access: "public-read-only" },
    { headers: { "Cache-Control": "no-store" } },
  );
}

export async function POST(_request: NextRequest) {
  return NextResponse.json({ error: "Password login is disabled" }, { status: 410 });
}

export async function DELETE() {
  return NextResponse.json({ authenticated: true });
}
