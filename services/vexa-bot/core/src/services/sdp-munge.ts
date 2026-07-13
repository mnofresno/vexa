/**
 * services/sdp-munge.ts — SDP-level video blocking script for browser injection.
 *
 * Returns a string (browser JS) that defines `sdpMungeDisableVideo` on the
 * window scope. Callers (screen-content.ts) use this to patch setLocal/Remote
 * description before the native WebRTC stack processes the SDP.
 *
 * Strategy: set m=video port to 0 (inactive) and strip codec attributes,
 * while preserving BUNDLE and rtcp-mux to avoid Chrome renegotiation crashes.
 */

export function getSdpMungeScript(): string {
  return `
    function sdpMungeDisableVideo(sdp) {
      var lines = sdp.split('\\n');
      var result = [];
      var inVideo = false;
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        if (line.startsWith('m=video')) {
          inVideo = true;
          result.push('m=video 0 UDP/TLS/RTP/SAVPF 0');
          continue;
        }
        if (inVideo && (line === '' || line.startsWith('m='))) {
          inVideo = false;
        }
        if (inVideo) {
          if (line.startsWith('a=sendrecv') || line.startsWith('a=recvonly') || line.startsWith('a=sendonly') || line.startsWith('a=active')) {
            result.push('a=inactive');
            continue;
          }
          if (line.startsWith('a=rtpmap:') || line.startsWith('a=rtcp-fb:') || line.startsWith('a=fmtp:') || line.startsWith('a=ssrc:')) {
            continue;
          }
          // Keep BUNDLE, rtcp-mux, mid, setup, fingerprint, etc.
        }
        result.push(line);
      }
      return result.join('\\n');
    }
  `;
}
