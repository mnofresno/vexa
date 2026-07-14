"use client";

import { useRef, useState } from "react";
import { CheckCircle2, Loader2, Video } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { toast } from "sonner";
import { withBasePath } from "@/lib/base-path";
import { vexaAPI } from "@/lib/api";
import { useAuthStore } from "@/stores/auth-store";

export function CreateGoogleMeet() {
  const user = useAuthStore((state) => state.user);
  const [isCreating, setIsCreating] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [readyToJoin, setReadyToJoin] = useState(false);
  const pendingWindow = useRef<Window | null>(null);

  async function startGoogleOAuth() {
    const response = await fetch(withBasePath("/api/calendar/oauth/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userEmail: user?.email, returnTo: "/join" }),
    });
    const data = await response.json();
    if (!response.ok || !data.authUrl) throw new Error(data.error || "Could not start Google authorization");
    window.location.assign(data.authUrl);
  }

  async function createMeeting() {
    if (!user?.email) return;
    setIsCreating(true);
    setReadyToJoin(false);
    setStatusMessage("Creating a new Google Meet...");

    // Open the tab synchronously from the user's click so browser popup
    // blockers do not prevent us from navigating it once the bot is active.
    pendingWindow.current = window.open("about:blank", "_blank");
    try {
      const response = await fetch(withBasePath("/api/google-meet/create"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ userEmail: user.email }),
      });
      const data = await response.json();
      if (response.status === 401 && data.code === "GOOGLE_MEET_OAUTH_REQUIRED") {
        pendingWindow.current?.close();
        pendingWindow.current = null;
        await startGoogleOAuth();
        return;
      }
      if (!response.ok) throw new Error(data.error || "Could not create Google Meet");

      const meetingId = data.meeting?.id;
      if (!meetingId) throw new Error("Google Meet was created but no bot session was returned");

      setStatusMessage("EC Listener is joining the new Meet...");
      const deadline = Date.now() + 120_000;
      let lastStatus = "requested";
      while (Date.now() < deadline) {
        const meeting = await vexaAPI.getMeeting(String(meetingId));
        lastStatus = meeting.status;
        if (meeting.status === "active") {
          setStatusMessage("EC Listener joined. Opening Google Meet...");
          setReadyToJoin(true);
          if (pendingWindow.current && !pendingWindow.current.closed) {
            pendingWindow.current.location.href = data.meeting_url;
          } else {
            window.open(data.meeting_url, "_blank", "noopener,noreferrer");
          }
          toast.success("EC Listener joined", { description: "Opening the new Google Meet." });
          return;
        }
        if (["failed", "completed", "stopped"].includes(meeting.status)) {
          throw new Error(`The bot could not join the new Meet (status: ${meeting.status})`);
        }
        setStatusMessage(lastStatus === "awaiting_admission" ? "EC Listener is waiting to enter..." : "EC Listener is connecting...");
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
      throw new Error("EC Listener did not join within two minutes");
    } catch (error) {
      pendingWindow.current?.close();
      pendingWindow.current = null;
      setStatusMessage(null);
      toast.error("Could not create Google Meet", { description: (error as Error).message });
    } finally {
      setIsCreating(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Create a new Google Meet</CardTitle>
        <CardDescription>Grainbox creates the room, starts EC Listener, waits for it to join, then opens the Meet.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Button type="button" onClick={() => void createMeeting()} disabled={isCreating || !user?.email}>
          {isCreating ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Video className="mr-2 h-4 w-4" />}
          {isCreating ? "Creating Meet and joining bot..." : "Create Meet + join with EC Listener"}
        </Button>
        {statusMessage && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
            {readyToJoin ? <CheckCircle2 className="h-4 w-4 text-green-500" /> : <Loader2 className="h-4 w-4 animate-spin" />}
            {statusMessage}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
