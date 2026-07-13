/**
 * services/transcription-telemetry.ts
 * Real-time transcription pipeline latency & diagnostic monitoring.
 *
 * Metrics:
 *   audio_capture_ms   — browser capture to bot queue
 *   voxtral_inference_ms — inference time (from backend response)
 *   network_latency_ms  — HTTP round-trip minus inference
 *   total_delay_ms      — end-to-end latency
 *
 * Publishes aggregated stats to Redis every 10s.
 * Warns when total_delay_ms > chunk_duration_ms (not real-time).
 */

import { logJSON } from '../utils/log';
import { createClient, RedisClientType } from 'redis';

export interface TelemetryMetrics {
  audio_capture_ms: number;
  voxtral_inference_ms: number;
  network_latency_ms: number;
  total_delay_ms: number;
}

export interface TelemetrySummary {
  ts: string;
  level: 'info' | 'warn';
  msg: string;
  chunk_duration_ms: number;
  inference_ms: number;
  network_ms: number;
  total_latency_ms: number;
  sample_count: number;
  warning?: string;
}

export class TranscriptionTelemetry {
  private metrics: TelemetryMetrics[] = [];
  private timer: NodeJS.Timeout | null = null;
  private readonly intervalMs: number;
  private readonly chunkDurationMs: number;
  private active = true;
  private redisUrl: string;
  private client: RedisClientType | null = null;

  constructor(intervalMs = 10000, chunkDurationMs = 3000) {
    this.intervalMs = intervalMs;
    this.chunkDurationMs = chunkDurationMs;
    this.redisUrl = process.env.REDIS_URL || 'redis://localhost:6379';
    this.start();
  }

  record(m: TelemetryMetrics): void {
    if (!this.active) return;
    this.metrics.push(m);
    if (m.total_delay_ms > this.chunkDurationMs) {
      logJSON({
        level: 'warn',
        msg: '[transcription-telemetry] total_delay exceeds chunk duration — not real-time',
        total_delay_ms: m.total_delay_ms,
        chunk_duration_ms: this.chunkDurationMs,
        audio_capture_ms: m.audio_capture_ms,
        voxtral_inference_ms: m.voxtral_inference_ms,
        network_latency_ms: m.network_latency_ms,
      });
    }
  }

  start(): void {
    if (this.timer) return;
    this.active = true;
    this.timer = setInterval(() => this.publish(), this.intervalMs);
    if (this.timer.unref) this.timer.unref();
  }

  stop(): void {
    this.active = false;
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
    this.publish();
    if (this.client) { this.client.disconnect().catch(() => {}); this.client = null; }
  }

  private publish(): void {
    if (this.metrics.length === 0) return;
    const samples = this.metrics.splice(0);
    const n = samples.length;
    const avgInference = samples.reduce((s, m) => s + m.voxtral_inference_ms, 0) / n;
    const avgNetwork = samples.reduce((s, m) => s + m.network_latency_ms, 0) / n;
    const avgTotal = samples.reduce((s, m) => s + m.total_delay_ms, 0) / n;

    const summary: TelemetrySummary = {
      ts: new Date().toISOString(),
      level: avgTotal > this.chunkDurationMs ? 'warn' : 'info',
      msg: 'Transcription segment telemetry',
      chunk_duration_ms: this.chunkDurationMs,
      inference_ms: Math.round(avgInference),
      network_ms: Math.round(avgNetwork),
      total_latency_ms: Math.round(avgTotal),
      sample_count: n,
    };
    if (avgTotal > this.chunkDurationMs) {
      summary.warning = `total_latency (${Math.round(avgTotal)}ms) > chunk_duration (${this.chunkDurationMs}ms)`;
    }

    logJSON({
      level: summary.level as 'info' | 'warn',
      msg: summary.msg,
      chunk_duration_ms: summary.chunk_duration_ms,
      inference_ms: summary.inference_ms,
      network_ms: summary.network_ms,
      total_latency_ms: summary.total_latency_ms,
      sample_count: summary.sample_count,
      warning: summary.warning,
    });
    this.pushToRedis(summary).catch(() => {});
  }

  private async pushToRedis(summary: TelemetrySummary): Promise<void> {
    if (!this.client) {
      this.client = createClient({ url: this.redisUrl });
      await this.client.connect();
    }
    await this.client.publish('tc:stats:transcription-pipeline', JSON.stringify(summary));
  }
}
