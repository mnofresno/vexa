import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Meeting } from "@/types/vexa";

const { getMeetingWithTranscripts } = vi.hoisted(() => ({
  getMeetingWithTranscripts: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  vexaAPI: { getMeetingWithTranscripts },
  VexaAPIError: class VexaAPIError extends Error {
    status = 500;
  },
}));

import { useMeetingsStore } from "@/stores/meetings-store";

const meeting: Meeting = {
  id: "16",
  platform: "google_meet",
  platform_specific_id: "upq-nvos-tpq",
  status: "completed",
  start_time: null,
  end_time: null,
  bot_container_id: null,
  data: {},
  created_at: "2026-07-12T16:51:46Z",
};

describe("meetings store transcript bootstrap", () => {
  beforeEach(() => {
    getMeetingWithTranscripts.mockReset();
    useMeetingsStore.setState({
      currentMeeting: meeting,
      meetings: [meeting],
      transcripts: [],
      recordings: [],
      error: null,
    });
  });

  it("refreshes post-meeting data returned with transcripts", async () => {
    getMeetingWithTranscripts.mockResolvedValue({
      meeting: {
        ...meeting,
        data: { ai_notes: { summary: "Generated notes" } },
      },
      segments: [],
      recordings: [],
    });

    await useMeetingsStore
      .getState()
      .fetchTranscripts("google_meet", "upq-nvos-tpq", "16");

    expect(useMeetingsStore.getState().currentMeeting?.data.ai_notes).toEqual({
      summary: "Generated notes",
    });
  });
});
