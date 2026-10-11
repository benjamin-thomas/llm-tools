// Reads the Mistral Vibe budget from inside Chrome, where the session cookie
// and Cloudflare's clearance already live, and hands only the budget to the
// native host (quota_gauges/native_host.py), which keeps it on disk for
// quota-gauges and token-recap. Mistral offers no other way in: an API key
// gets a 401, and a script outside a browser gets a Cloudflare challenge.

const HOST = "dev.benjamin.quota_gauges";
const BUDGET_URL = "https://chat.mistral.ai/api/trpc/billing.budget";
const ALARM = "budget";
const EVERY_MINUTES = 1;

function schedule() {
  chrome.alarms.create(ALARM, { delayInMinutes: 0.1, periodInMinutes: EVERY_MINUTES });
}

chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) refresh();
});

// tRPC wraps the answer as result.data.json (superjson), or in an array when
// batched; take the budget from either and reject anything else.
function budgetOf(body) {
  const one = Array.isArray(body) ? body[0] : body;
  const data = one && one.result && one.result.data;
  const budget = data && "json" in data ? data.json : data;
  return budget && typeof budget === "object" && "usage_percentage" in budget ? budget : null;
}

async function refresh() {
  const message = { fetched_at: new Date().toISOString() };
  try {
    const response = await fetch(BUDGET_URL, { credentials: "include" });
    if (!response.ok) {
      // 401 is the usual one: the chat.mistral.ai session has expired.
      message.error = `HTTP ${response.status}`;
    } else {
      const budget = budgetOf(await response.json());
      if (budget) message.budget = budget;
      else message.error = "unexpected answer";
    }
  } catch (err) {
    message.error = String(err).slice(0, 200);
  }
  chrome.runtime.sendNativeMessage(HOST, message, () => {
    // Read it so Chrome does not log an unchecked error when the host is missing.
    void chrome.runtime.lastError;
  });
}
