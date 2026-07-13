import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  compatible: vi.fn((model: string) => ({ type: "compatible", model })),
  chat: vi.fn((model: string) => ({ type: "chat", model })),
  responses: vi.fn((model: string) => ({ type: "responses", model })),
}));

vi.mock("@ai-sdk/openai-compatible", () => ({
  createOpenAICompatible: vi.fn(() => mocks.compatible),
}));

vi.mock("@ai-sdk/openai", () => ({
  createOpenAI: vi.fn(() =>
    Object.assign(mocks.responses, {
      chat: mocks.chat,
    })
  ),
}));

import { getModel } from "../src/app/api/ai/chat/route";

const originalEnv = { ...process.env };

afterEach(() => {
  process.env = { ...originalEnv };
  vi.clearAllMocks();
});

describe("getModel", () => {
  it("uses chat completions for local OpenAI-compatible providers", () => {
    process.env.AI_MODEL = "local/qwen-local";
    process.env.AI_API_KEY = "test-key";
    process.env.AI_BASE_URL = "http://localhost:4000/v1";

    expect(getModel()).toEqual({ type: "compatible", model: "qwen-local" });
    expect(mocks.compatible).toHaveBeenCalledWith("qwen-local");
    expect(mocks.chat).not.toHaveBeenCalled();
    expect(mocks.responses).not.toHaveBeenCalled();
  });
});
