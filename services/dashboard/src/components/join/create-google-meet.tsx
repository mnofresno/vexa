"use client";

import { useEffect, useState } from "react";
import { ExternalLink, Loader2, Video } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { toast } from "sonner";
import { withBasePath } from "@/lib/base-path";
import { useAuthStore } from "@/stores/auth-store";

export function CreateGoogleMeet() {
  const user = useAuthStore((state) => state.user);
  const [isCreating, setIsCreating] = useState(false);
  const [meetingUrl, setMeetingUrl] = useState<string | null>(null);

  async function startGoogleOAuth() {
    const response = await fetch(withBasePath("/api/calendar/oauth/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userEmail: user?.email, returnTo: "/join?create_google_meet=1" }),
    });
    const data = await response.json();
    if (!response.ok || !data.authUrl) throw new Error(data.error || "Could not start Google authorization");
    window.location.assign(data.authUrl);
  }

  async function createMeeting() {
    if (!user?.email) return;
    setIsCreating(true);
    try {
      const response = await fetch(withBasePath("/api/google-meet/create"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ userEmail: user.email }),
      });
      const data = await response.json();
      if (response.status === 401 && data.code === "GOOGLE_MEET_OAUTH_REQUIRED") {
        await startGoogleOAuth();
        return;
      }
      if (!response.ok) throw new Error(data.error || "Could not create Google Meet");
      setMeetingUrl(data.meeting_url);
      toast.success("Google Meet created", { description: "The transcription bot is joining now." });
    } catch (error) {
      toast.error("Could not create Google Meet", { description: (error as Error).message });
    } finally {
      setIsCreating(false);
    }
  }

  useEffect(() => {
    if (typeof window !== "undefined" && new URLSearchParams(window.location.search).get("create_google_meet") === "1") {
      void createMeeting();
    }
    // OAuth callback is a one-shot action; user identity is stable for this page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.email]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Create a Google Meet</CardTitle>
        <CardDescription>Grainbox creates the room, starts the bot, and gives you the link.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Button type="button" onClick={() => void createMeeting()} disabled={isCreating || !user?.email}>
          {isCreating ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Video className="mr-2 h-4 w-4" />}
          {isCreating ? "Creating..." : "Create Meet and start bot"}
        </Button>
        {meetingUrl && (
          <a className="flex items-center gap-2 text-sm text-primary underline" href={meetingUrl} target="_blank" rel="noreferrer">
            {meetingUrl}
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        )}
      </CardContent>
    </Card>
  );
}
