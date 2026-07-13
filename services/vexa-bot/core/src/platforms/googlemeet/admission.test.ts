import assert from "node:assert/strict";

import { checkForGoogleAdmissionIndicators } from "./admission";
import { checkEscalation, resetEscalation } from "../shared/escalation";

function pageWithVisibleSelectors(selectors: string[]) {
  const visible = new Set(selectors);
  return {
    locator(selector: string) {
      return {
        first() {
          return {
            async isVisible() {
              return visible.has(selector);
            },
            async getAttribute() {
              return null;
            },
          };
        },
      };
    },
  };
}

async function main(): Promise<void> {
  const lobbyText = 'text*="Please wait until a meeting host brings you into the call"';

  assert.equal(
    await checkForGoogleAdmissionIndicators(
      pageWithVisibleSelectors([lobbyText, "[data-participant-id]"]) as any,
    ),
    false,
    "waiting-room self tile must not count as admission",
  );

  assert.equal(
    await checkForGoogleAdmissionIndicators(
      pageWithVisibleSelectors([
        'button:has-text("Ask to join")',
        'button[aria-label*="People"]',
      ]) as any,
    ),
    false,
    "visible pre-join controls must suppress admission",
  );

  assert.equal(
    await checkForGoogleAdmissionIndicators(
      pageWithVisibleSelectors(['button[aria-label*="People"]']) as any,
    ),
    true,
    "in-meeting people control should confirm admission",
  );

  resetEscalation();
  assert.equal(
    checkEscalation(12_000, 900_000, 12_000),
    null,
    "normal lobby transitions must not escalate after ten seconds",
  );
  assert.equal(
    checkEscalation(32_000, 900_000, 32_000)?.reason,
    "unknown_blocking_state",
    "a sustained unknown state should still escalate",
  );

  console.log("google meet admission classification: ok");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
