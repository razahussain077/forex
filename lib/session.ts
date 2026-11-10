import { createHmac, timingSafeEqual } from "node:crypto";

export const SESSION_COOKIE = "swiftlabor_session";
export const SESSION_TTL_SECONDS = 60 * 60 * 8;

function sessionSecret(): string {
  const value = process.env.SESSION_SECRET || "";
  if (value.length < 32) {
    throw new Error("SESSION_SECRET must be configured with at least 32 characters");
  }
  return value;
}

export function createSessionToken(now = Math.floor(Date.now() / 1000)): string {
  const payload = Buffer.from(JSON.stringify({
    sub: "dashboard",
    iat: now,
    exp: now + SESSION_TTL_SECONDS,
  })).toString("base64url");
  const signature = createHmac("sha256", sessionSecret()).update(payload).digest("base64url");
  return payload + "." + signature;
}

export function verifySessionToken(token: string | undefined, now = Math.floor(Date.now() / 1000)): boolean {
  if (!token || token.length > 2048) return false;
  const parts = token.split(".");
  if (parts.length !== 2) return false;
  const [payload, supplied] = parts;
  let expected: Buffer;
  let received: Buffer;
  try {
    expected = createHmac("sha256", sessionSecret()).update(payload).digest();
    received = Buffer.from(supplied, "base64url");
    if (received.length !== expected.length || !timingSafeEqual(received, expected)) return false;
    const decoded = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as {
      sub?: string; exp?: number; iat?: number;
    };
    return decoded.sub === "dashboard"
      && Number.isInteger(decoded.exp)
      && Number.isInteger(decoded.iat)
      && (decoded.exp ?? 0) > now
      && (decoded.iat ?? now) <= now + 60;
  } catch {
    return false;
  }
}

