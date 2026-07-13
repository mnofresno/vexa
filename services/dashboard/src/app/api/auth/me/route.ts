import { NextRequest, NextResponse } from "next/server";
import { cookies } from "next/headers";

const VEXA_TOKEN_COOKIE = "vexa-token";
const VEXA_USER_INFO_COOKIE = "vexa-user-info";

type VexaUser = {
  id: number;
  email: string;
  name?: string | null;
  max_concurrent_bots?: number;
  created_at?: string;
};

function adminHeaders(): HeadersInit {
  return {
    "Content-Type": "application/json",
    "X-Admin-API-Key": process.env.VEXA_ADMIN_API_KEY || "",
  };
}

async function provisionFromAuthentik(email: string, name: string): Promise<{ user: VexaUser; token: string }> {
  const adminUrl = process.env.VEXA_ADMIN_API_URL || process.env.VEXA_API_URL || "http://localhost:8056";
  const adminKey = process.env.VEXA_ADMIN_API_KEY || "";
  if (!adminKey) throw new Error("VEXA_ADMIN_API_KEY is not configured");

  const lookup = await fetch(`${adminUrl}/admin/users/email/${encodeURIComponent(email)}`, {
    headers: adminHeaders(),
    cache: "no-store",
  });

  let user: VexaUser;
  if (lookup.ok) {
    user = await lookup.json() as VexaUser;
  } else if (lookup.status === 404) {
    const created = await fetch(`${adminUrl}/admin/users`, {
      method: "POST",
      headers: adminHeaders(),
      body: JSON.stringify({ email, name, max_concurrent_bots: 5 }),
      cache: "no-store",
    });
    if (!created.ok) throw new Error(`Could not create Vexa user (${created.status})`);
    user = await created.json() as VexaUser;
  } else {
    throw new Error(`Could not resolve Vexa user (${lookup.status})`);
  }

  if (name && name !== user.name) {
    const synced = await fetch(`${adminUrl}/admin/users/${user.id}`, {
      method: "PATCH",
      headers: adminHeaders(),
      body: JSON.stringify({ name }),
      cache: "no-store",
    });
    if (synced.ok) user = await synced.json() as VexaUser;
  }

  const details = await fetch(`${adminUrl}/admin/users/${user.id}`, {
    headers: adminHeaders(),
    cache: "no-store",
  });
  if (!details.ok) throw new Error(`Could not load Vexa user tokens (${details.status})`);
  const detail = await details.json() as { api_tokens?: Array<{ token: string; scopes?: string[] }> };
  const existing = detail.api_tokens?.find((candidate) => {
    const scopes = new Set(candidate.scopes || []);
    return scopes.has("bot") && scopes.has("tx") && scopes.has("browser");
  });

  let token = existing?.token;
  if (!token) {
    const createdToken = await fetch(
      `${adminUrl}/admin/users/${user.id}/tokens?scopes=bot,tx,browser&name=bpf-auth-sso`,
      { method: "POST", headers: adminHeaders(), cache: "no-store" },
    );
    if (!createdToken.ok) throw new Error(`Could not create Vexa token (${createdToken.status})`);
    token = (await createdToken.json() as { token?: string }).token;
  }
  if (!token) throw new Error("Vexa did not return an API token");

  return { user, token };
}

/**
 * Get current user info from token.
 * Auth chain: cookie only. No fallback to env vars.
 * User identity resolved via gateway /auth/me.
 */
export async function GET(request: NextRequest) {
  const VEXA_API_URL = process.env.VEXA_API_URL || "http://localhost:8056";

  const cookieStore = await cookies();
  const cookieToken = cookieStore.get("vexa-token")?.value;
  const authentikAuthenticated = request.headers.get("x-authentik-authenticated") === "true";
  const authentikEmail = request.headers.get("x-authentik-email")?.trim().toLowerCase() || "";
  const authentikName = request.headers.get("x-authentik-username")?.trim() || authentikEmail;

  let token = cookieToken || "";

  if (token) try {
    // Resolve user identity via gateway /auth/me
    const response = await fetch(`${VEXA_API_URL}/auth/me`, {
      headers: { "X-API-Key": token },
    });

    if (!response.ok) {
      if (cookieToken) cookieStore.delete(VEXA_TOKEN_COOKIE);
      token = "";
    } else {
      const data = await response.json();
      const user = {
        id: data.user_id,
        email: data.email,
        name: data.name || data.email,
      };
      return NextResponse.json({ authenticated: true, user, token });
    }
  } catch (error) {
    console.error("Vexa token verification error:", error);
    token = "";
  }

  if (authentikAuthenticated && authentikEmail) {
    try {
      const provisioned = await provisionFromAuthentik(authentikEmail, authentikName);
      const response = NextResponse.json({
        authenticated: true,
        user: provisioned.user,
        token: provisioned.token,
        identityProvider: "bpf-auth",
      });
      response.cookies.set(VEXA_TOKEN_COOKIE, provisioned.token, {
        httpOnly: true,
        secure: process.env.NODE_ENV === "production",
        sameSite: "lax",
        path: "/",
        maxAge: 60 * 60 * 24 * 30,
      });
      response.cookies.set(VEXA_USER_INFO_COOKIE, JSON.stringify(provisioned.user), {
        httpOnly: true,
        secure: process.env.NODE_ENV === "production",
        sameSite: "lax",
        path: "/",
        maxAge: 60 * 60 * 24 * 30,
      });
      return response;
    } catch (error) {
      console.error("Authentik-to-Vexa provisioning error:", error);
      return NextResponse.json(
        { error: "Could not synchronize the bpf-auth user with Grainbox" },
        { status: 502 },
      );
    }
  }

  return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
}
