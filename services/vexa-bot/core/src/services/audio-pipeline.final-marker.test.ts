import assert from "node:assert/strict";
import { EventEmitter } from "node:events";

import {
  type AudioCaptureSource,
  type AudioChunk,
  UnifiedRecordingPipeline,
} from "./audio-pipeline";

class FinalMarkerSource extends EventEmitter implements AudioCaptureSource {
  async start(): Promise<void> {}

  async stop(): Promise<void> {
    const finalMarker: AudioChunk = {
      format: "webm",
      data: Buffer.alloc(0),
      seq: 4,
      isFinal: true,
    };
    this.emit("chunk", finalMarker);
  }
}

async function main(): Promise<void> {
  const uploads: Array<{ size: number; seq: number; isFinal: boolean }> = [];
  const recordingService = {
    async uploadChunk(
      _url: string,
      _token: string,
      data: Buffer,
      seq: number,
      isFinal: boolean,
    ): Promise<void> {
      uploads.push({ size: data.length, seq, isFinal });
    },
  };

  const pipeline = new UnifiedRecordingPipeline({
    source: new FinalMarkerSource(),
    recordingService: recordingService as any,
    uploadUrl: "http://meeting-api/internal/recordings/upload",
    token: "test-token",
    platform: "gmeet",
  });

  await pipeline.start();
  await pipeline.stop();

  assert.deepEqual(uploads, [{ size: 0, seq: 4, isFinal: true }]);
  console.log("audio-pipeline final marker: ok");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
