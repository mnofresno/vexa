import { createClient, RedisClientType } from 'redis';
import { log } from '../utils';

export interface TranscriptionSegment {
  /** Stable segment identity — required for versioned envelopes */
  id: string;
  speaker: string;
  text: string;
  /** Relative to session start (seconds) */
  start: number;
  /** Relative to session start (seconds) */
  end: number;
  language: string;
  /** Whether this segment is finalized */
  completed?: boolean;
  /** Absolute UTC start time as ISO string */
  absolute_start_time?: string;
  /** Absolute UTC end time as ISO string */
  absolute_end_time?: string;
  /** Stable segment identity: {session_uid}:{speakerId}:{sequenceNumber} */
  segment_id?: string;
  /** Source of this segment: 'audio' (Whisper), 'caption' (Teams ASR), or 'merged' */
  source?: 'audio' | 'caption' | 'merged';
  /** Raw caption text from Teams ASR (if available) */
  caption_text?: string;
  /** How the speaker was determined: 'dom' (blue squares), 'caption', or 'both' */
  speaker_source?: 'dom' | 'caption' | 'both';
}

export interface SpeakerEvent {
  speaker: string;
  type: 'joined' | 'left' | 'started_speaking' | 'stopped_speaking';
  timestamp: number;
}

export interface SegmentPublisherConfig {
  /** Redis URL, e.g. "redis://localhost:6379" */
  redisUrl: string;
  /** Internal meeting ID (numeric) */
  meetingId: string;
  /** MeetingToken JWT (HS256, signed by meeting-api) */
  token: string;
  /** Session UID (connectionId from bot config) */
  sessionUid: string;
  /** Platform identifier */
  platform: string;
  /** Redis stream key for transcription segments. Default: "transcription_segments" */
  segmentStreamKey?: string;
  /** Redis stream key for speaker events. Default: "speaker_events_relative" */
  speakerEventStreamKey?: string;
}

/**
 * Publishes transcription segments and speaker events to Redis
 * in the format expected by transcription-collector.
 *
 * Segments: XADD with { payload: JSON } to stream, PUBLISH flat JSON to pub/sub.
 * Speaker events: XADD flat fields to speaker_events_relative stream.
 */

/** Schema version for the published envelope — bumped when the wire format changes */
const ENVELOPE_SCHEMA_VERSION = 1;

/** Maximum retry attempts for transient Redis failures */
const MAX_RETRY_ATTEMPTS = 3;

/** Transient error patterns that should be retried */
const TRANSIENT_ERRORS = [
  'ECONNRESET',
  'ECONNREFUSED',
  'ETIMEDOUT',
  'EAI_AGAIN',
  'ENETUNREACH',
  'Connection is closed',
  'WRONGTYPE',
  'IOError',
];

/** Counts for published, retried and permanently failed bundles */
export interface PublicationCounters {
  published: number;
  retried: number;
  failed: number;
}

/** Telemetry record for a permanent publication failure */
export interface PublicationFailureTelemetry {
  segmentId: string;
  meetingId: string;
  sessionUid: string;
  reason: string;
  attempts: number;
  timestamp: string;
}

export class SegmentPublisher {
  private redisUrl: string;
  private meetingId: string;
  private token: string;
  readonly sessionUid: string;
  private platform: string;
  private segmentStreamKey: string;
  private speakerEventStreamKey: string;
  private client: RedisClientType | null = null;
  private connected: boolean = false;
  /** Wall-clock time when the session started (ms). Defaults to construction time,
   *  should be reset via resetSessionStart() when audio capture actually begins
   *  so that segment start_time aligns with the recording. */
  sessionStartMs: number;

  // Publication counters (requirement: export counters for published, retried, failed)
  private _counters: PublicationCounters = { published: 0, retried: 0, failed: 0 };

  /** Bot lifecycle telemetry — records permanent failures with reasons */
  private _failureTelemetry: PublicationFailureTelemetry[] = [];

  constructor(config: SegmentPublisherConfig) {
    this.redisUrl = config.redisUrl;
    this.meetingId = config.meetingId;
    this.token = config.token;
    this.sessionUid = config.sessionUid;
    this.platform = config.platform;
    this.segmentStreamKey = config.segmentStreamKey ?? 'transcription_segments';
    this.speakerEventStreamKey = config.speakerEventStreamKey ?? 'speaker_events_relative';
    this.sessionStartMs = Date.now();
  }

  /**
   * Return a snapshot of publication counters (published, retried, failed).
   * Requirement: export counters for published, retried and permanently failed bundles.
   */
  get counters(): Readonly<PublicationCounters> {
    return { ...this._counters };
  }

  /**
   * Return the recorded failure telemetry for bot lifecycle reporting.
   */
  get failureTelemetry(): ReadonlyArray<PublicationFailureTelemetry> {
    return [...this._failureTelemetry];
  }

  /**
   * Determine whether an error is transient and worth retrying.
   */
  private isTransientError(err: unknown): boolean {
    const msg = (err as any)?.message ?? (err as any)?.code ?? String(err);
    return TRANSIENT_ERRORS.some((pattern) => msg.includes(pattern));
  }

  /**
   * Retry a Redis operation with bounded attempts for transient failures.
   * On permanent failure, records the reason into bot lifecycle telemetry.
   *
   * @returns true if the operation succeeded (possibly after retries).
   */
  private async retryWithBackoff(
    operation: () => Promise<void>,
    segmentId: string,
  ): Promise<boolean> {
    let lastError: string = 'unknown';
    let attempts = 1;

    while (attempts <= MAX_RETRY_ATTEMPTS) {
      try {
        await operation();
        return true;
      } catch (err: any) {
        lastError = err?.message ?? String(err);
        if (!this.isTransientError(err)) {
          break; // non-transient — no point retrying
        }
        if (attempts < MAX_RETRY_ATTEMPTS) {
          this._counters.retried++;
          const delay = 200 * Math.pow(2, attempts - 1); // 200, 400, 800 ms
          log(`[SegmentPublisher] Transient Redis failure (attempt ${attempts}), retrying in ${delay}ms: ${lastError}`);
          await new Promise((r) => setTimeout(r, delay));
        }
        attempts++;
      }
    }

    // Permanent failure — record telemetry
    this._counters.failed++;
    const telemetry: PublicationFailureTelemetry = {
      segmentId,
      meetingId: this.meetingId,
      sessionUid: this.sessionUid,
      reason: lastError,
      attempts,
      timestamp: new Date().toISOString(),
    };
    this._failureTelemetry.push(telemetry);
    log(`[SegmentPublisher] Permanent failure for segment ${segmentId} after ${attempts} attempt(s): ${lastError}`);
    return false;
  }

  /**
   * Reset session start to now. Call when audio capture actually begins
   * so that segment start_time values align with the recording timeline.
   */
  resetSessionStart(): void {
    this.sessionStartMs = Date.now();
  }

  /**
   * Ensure the Redis client is connected. Creates and connects on first call.
   */
  private async ensureConnected(): Promise<RedisClientType> {
    if (this.client && this.connected) {
      return this.client;
    }

    try {
      this.client = createClient({
        url: this.redisUrl,
        socket: { connectTimeout: 5000 },
      }) as RedisClientType;

      this.client.on('error', (err) => {
        log(`[SegmentPublisher] Redis client error: ${err.message}`);
        this.connected = false;
      });

      // Race connection against a 5s timeout
      const connectPromise = this.client.connect();
      const timeoutPromise = new Promise((_, reject) =>
        setTimeout(() => reject(new Error('Redis connect timeout (5s)')), 5000)
      );
      await Promise.race([connectPromise, timeoutPromise]);

      this.connected = true;
      log(`[SegmentPublisher] Connected to Redis at ${this.redisUrl}`);
      return this.client;
    } catch (err: any) {
      log(`[SegmentPublisher] Failed to connect to Redis: ${err.message}`);
      this.connected = false;
      throw err;
    }
  }

  /**
   * Publish a session_start message to the stream.
   * Called once when the per-speaker pipeline is initialized.
   */
  async publishSessionStart(): Promise<void> {
    try {
      const client = await this.ensureConnected();

      const payload = JSON.stringify({
        type: 'session_start',
        token: this.token,
        uid: this.sessionUid,
        platform: this.platform,
        meeting_id: this.meetingId,
        start_timestamp: new Date(this.sessionStartMs).toISOString(),
      });

      await client.xAdd(this.segmentStreamKey, '*', { payload });
      log(`[SegmentPublisher] Published session_start for session ${this.sessionUid}`);
    } catch (err: any) {
      log(`[SegmentPublisher] Failed to publish session_start: ${err.message}`);
    }
  }

  /**
   * Publish a session_end message to the stream.
   * Called on pipeline cleanup.
   */
  async publishSessionEnd(): Promise<void> {
    try {
      const client = await this.ensureConnected();

      const payload = JSON.stringify({
        type: 'session_end',
        token: this.token,
        uid: this.sessionUid,
      });

      await client.xAdd(this.segmentStreamKey, '*', { payload });
      log(`[SegmentPublisher] Published session_end for session ${this.sessionUid}`);
    } catch (err: any) {
      log(`[SegmentPublisher] Failed to publish session_end: ${err.message}`);
    }
  }

  /**
   * Publish a transcription segment to Redis.
   *
   * PR3 requirements:
   * - Versioned envelope with schema_version, type, meeting_id, session_uid, segments[].
   * - Requires meeting_id, session_uid and segment id; never publishes anonymous bundles.
   * - Awaits Redis acknowledgement before marking a segment published.
   * - Retries transient Redis failures; records final failure reason in telemetry.
   * - Updates published / retried / failed counters.
   *
   * @returns true when the XADD acknowledgement was received (possibly after retries).
   */
  async publishSegment(segment: TranscriptionSegment): Promise<boolean> {
    // Require meeting_id and session_uid — never publish an anonymous bundle
    if (!this.meetingId || !this.sessionUid) {
      const reason = `Missing required field: meeting_id=${!!this.meetingId}, session_uid=${!!this.sessionUid}`;
      log(`[SegmentPublisher] Refusing to publish segment: ${reason}`);
      this._counters.failed++;
      this._failureTelemetry.push({
        segmentId: segment.id ?? 'unknown',
        meetingId: this.meetingId,
        sessionUid: this.sessionUid,
        reason,
        attempts: 0,
        timestamp: new Date().toISOString(),
      });
      return false;
    }

    // Require segment id — never publish an anonymous segment
    if (!segment.id) {
      const reason = 'Segment missing required id field';
      log(`[SegmentPublisher] Refusing to publish anonymous segment: ${reason}`);
      this._counters.failed++;
      this._failureTelemetry.push({
        segmentId: 'unknown',
        meetingId: this.meetingId,
        sessionUid: this.sessionUid,
        reason,
        attempts: 0,
        timestamp: new Date().toISOString(),
      });
      return false;
    }

    // Build the versioned envelope
    const envelope = {
      schema_version: ENVELOPE_SCHEMA_VERSION,
      type: 'transcription',
      meeting_id: Number(this.meetingId),
      session_uid: this.sessionUid,
      segments: [{
        id: segment.id,
        start: segment.start,
        end: segment.end,
        text: segment.text,
        speaker: segment.speaker,
        language: segment.language,
        completed: segment.completed ?? true,
        ...(segment.absolute_start_time && { absolute_start_time: segment.absolute_start_time }),
        ...(segment.absolute_end_time && { absolute_end_time: segment.absolute_end_time }),
      }],
    };

    const segmentId = segment.id;

    // Retry transient failures; await acknowledgement before returning success
    const succeeded = await this.retryWithBackoff(async () => {
      const client = await this.ensureConnected();

      // XADD — acknowledgement is the return value (stream entry ID)
      const ack = await client.xAdd(this.segmentStreamKey, '*', {
        payload: JSON.stringify(envelope),
      });
      // If xAdd returns, Redis has acknowledged the write
      void ack;

      // PUBLISH: flat JSON for real-time delivery via gateway → WebSocket → dashboard
      const channel = `meeting:${this.meetingId}:segments`;
      await client.publish(channel, JSON.stringify({
        ...segment,
        meeting_id: Number(this.meetingId),
        timestamp: Date.now(),
        ...(segment.absolute_start_time && { absolute_start_time: segment.absolute_start_time }),
        ...(segment.absolute_end_time && { absolute_end_time: segment.absolute_end_time }),
      }));
    }, segmentId);

    if (succeeded) {
      this._counters.published++;
      log(`[SegmentPublisher] Published segment ${segmentId} (envelope v${ENVELOPE_SCHEMA_VERSION})`);
    }

    return succeeded;
  }

  /**
   * Publish a per-speaker transcript update: confirmed + pending in one atomic message.
   * - XADD confirmed to stream (collector persists to Postgres)
   * - PUBLISH bundle to WS channel (gateway forwards to dashboard)
   */
  async publishTranscript(speaker: string, confirmed: TranscriptionSegment[], pending: TranscriptionSegment[]): Promise<void> {
    try {
      const client = await this.ensureConnected();

      const mapSeg = (s: TranscriptionSegment) => ({
        id: s.id,
        start: s.start, end: s.end, text: s.text, language: s.language,
        completed: s.completed ?? true, speaker: s.speaker,
        ...(s.absolute_start_time && { absolute_start_time: s.absolute_start_time }),
        ...(s.absolute_end_time && { absolute_end_time: s.absolute_end_time }),
      });

      // XADD confirmed segments for persistence — versioned envelope per segment
      for (const seg of confirmed) {
        const envelope = {
          schema_version: ENVELOPE_SCHEMA_VERSION,
          type: 'transcription',
          meeting_id: Number(this.meetingId),
          session_uid: this.sessionUid,
          segments: [mapSeg(seg)],
        };
        await client.xAdd(this.segmentStreamKey, '*', {
          payload: JSON.stringify(envelope),
        });
      }

      // Store pending snapshot in Redis (full replace per speaker, short TTL)
      const pendingKey = `meeting:${this.meetingId}:pending:${speaker}`;
      if (pending.length > 0) {
        await client.set(pendingKey, JSON.stringify(pending.map(mapSeg)), { EX: 60 });
      } else {
        await client.del(pendingKey);
      }

      // PUBLISH atomic bundle directly to WS channel (no collector middleman)
      const wsChannel = `tc:meeting:${this.meetingId}:mutable`;
      await client.publish(wsChannel, JSON.stringify({
        type: 'transcript',
        meeting: { id: parseInt(this.meetingId) },
        speaker,
        confirmed: confirmed.map(mapSeg),
        pending: pending.map(mapSeg),
        ts: new Date().toISOString(),
      }));
    } catch (err: any) {
      log(`[SegmentPublisher] Failed to publish transcript: ${err.message}`);
    }
  }

  /**
   * Publish a speaker lifecycle event to Redis.
   * Format matches what transcription-collector expects:
   *   stream: speaker_events_relative
   *   fields: uid, relative_client_timestamp_ms, event_type, participant_name
   *
   * Errors are logged but do not throw.
   */
  async publishSpeakerEvent(event: SpeakerEvent): Promise<void> {
    try {
      const client = await this.ensureConnected();

      // Map bot event types to collector's expected event_type values
      const eventTypeMap: Record<string, string> = {
        'joined': 'SPEAKER_START',
        'started_speaking': 'SPEAKER_START',
        'stopped_speaking': 'SPEAKER_END',
        'left': 'SPEAKER_END',
      };

      const fields: Record<string, string> = {
        uid: this.sessionUid,
        relative_client_timestamp_ms: String(event.timestamp - this.sessionStartMs),
        event_type: eventTypeMap[event.type] || event.type,
        participant_name: event.speaker,
        meeting_id: this.meetingId,
      };

      await client.xAdd(this.speakerEventStreamKey, '*', fields);
    } catch (err: any) {
      log(`[SegmentPublisher] Failed to publish speaker event: ${err.message}`);
    }
  }

  /**
   * Disconnect from Redis and clean up.
   */
  async close(): Promise<void> {
    if (this.client) {
      try {
        await this.client.disconnect();
        log(`[SegmentPublisher] Redis connection closed`);
      } catch (err: any) {
        log(`[SegmentPublisher] Error closing Redis connection: ${err.message}`);
      }
      this.client = null;
      this.connected = false;
    }
  }
}
