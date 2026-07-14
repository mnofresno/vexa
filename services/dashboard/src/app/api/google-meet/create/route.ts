import { NextRequest, NextResponse } from "next/server";
import { cookies } from "next/headers";
import { findUserByEmail, updateUser } from "@/lib/vexa-admin-api";

const MEET_SCOPE = "https://www.googleapis.com/auth/meetings.space.created";

type StoredGoogleOAuth = {
  access_token?: string;
  refresh_token?: string;
  expires_at?: number;
  scope?: string;
};

function jsonError(error: string, status: number, extra: Record<string, unknown> = {}) {
  return NextResponse.json({ error, ...extra }, { status });
}

async function refreshGoogleAccessToken(
  oauth: StoredGoogleOAuth,
  userId: string,
  userData: Record<string, unknown>,
): Promise<string> {
  if (!oauth.refresh_token) {
    throw new Error("Google authorization is missing a refresh token");
  }

  const clientId = process.env.GOOGLE_CLIENT_ID || "";
  const clientSecret = process.env.GOOGLE_CLIENT_SECRET || "";
  if (!clientId || !clientSecret) {
    throw new Error("Google OAuth client is not configured");
  }

  const response = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: oauth.refresh_token,
      client_id: clientId,
      client_secret: clientSecret,
    }),
    cache: "no-store",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload.access_token) {
    throw new Error(`Google token refresh failed (${response.status})`);
  }

  const refreshedOAuth: StoredGoogleOAuth = {
    ...oauth,
    access_token: payload.access_token,
    expires_at: Math.floor(Date.now() / 1000) + Number(payload.expires_in || 3600),
    scope: payload.scope || oauth.scope,
  };
  const updatedData = {
    ...userData,
    google_calendar: { oauth: refreshedOAuth },
  };
  const saved = await updateUser(userId, { data: updatedData });
  if (!saved.success) {
    throw new Error(saved.error?.message || "Could not persist refreshed Google token");
  }
  return payload.access_token;
}

export async function POST(req: NextRequest) {
  try {
    await req.json().catch(() => ({}));
    const cookieStore = await cookies();
    const userToken = cookieStore.get("vexa-token")?.value;
    const userInfoCookie = cookieStore.get("vexa-user-info")?.value;
    let userEmail = "";
    try {
      userEmail = String(JSON.parse(userInfoCookie || "{}").email || "").toLowerCase();
    } catch {
      userEmail = "";
    }
    if (!userToken || !userEmail) {
      return jsonError("Not authenticated", 401);
    }

    const userResult = await findUserByEmail(userEmail);
    if (!userResult.success || !userResult.data) {
      return jsonError(userResult.error?.message || "Could not resolve user", 400);
    }

    const userData = userResult.data.data || {};
    const oauth = (userData.google_calendar as { oauth?: StoredGoogleOAuth } | undefined)?.oauth;
    if (!oauth?.refresh_token || !oauth.scope?.includes(MEET_SCOPE)) {
      return jsonError("Google Meet authorization is required", 401, {
        code: "GOOGLE_MEET_OAUTH_REQUIRED",
      });
    }

    let accessToken = oauth.access_token || "";
    const expiresAt = Number(oauth.expires_at || 0);
    if (!accessToken || expiresAt <= Math.floor(Date.now() / 1000) + 60) {
      accessToken = await refreshGoogleAccessToken(oauth, userResult.data.id, userData);
    }

    const spaceResponse = await fetch("https://meet.googleapis.com/v2/spaces", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({}),
      cache: "no-store",
    });
    const space = await spaceResponse.json().catch(() => ({}));
    if (!spaceResponse.ok || !space.meetingUri) {
      return jsonError(space.error?.message || "Google Meet space creation failed", 502);
    }

    const vexaApiUrl = process.env.VEXA_API_URL || "http://localhost:8066";
    const botResponse = await fetch(`${vexaApiUrl}/bots`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": userToken,
      },
      body: JSON.stringify({
        platform: "google_meet",
        meeting_url: space.meetingUri,
        authenticated: true,
        bot_name: process.env.DEFAULT_BOT_NAME || "EC Listener",
      }),
      cache: "no-store",
    });
    const meeting = await botResponse.json().catch(() => ({}));
    if (!botResponse.ok) {
      return jsonError(meeting.detail || "Could not start transcription bot", 502, {
        meeting_url: space.meetingUri,
      });
    }

    return NextResponse.json({ meeting_url: space.meetingUri, meeting });
  } catch (error) {
    return jsonError((error as Error).message || "Could not create Google Meet", 500);
  }
}
