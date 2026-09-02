#!/usr/bin/env ruby

# Symlink to ~/.claude/statusline.rb, then point settings.json at it:
#   "statusLine": { "type": "command", "command": "~/.claude/statusline.rb",
#                   "padding": 0, "refreshInterval": 60 }

require "json"
require "net/http"
require "time"
require "fileutils"

CYAN  = "\033[36m"
DIM   = "\033[2m"
GREEN = "\033[32m"
AMBER = "\033[33m"
RED   = "\033[31m"
RESET = "\033[0m"

# $/MTok base input, mirroring ../token_recap/native.py (authoritative; a
# render cannot afford to shell out to Python). A rebuild costs the write/read
# spread, not the prefix again. tests/test_claude_statusline_rates.py guards it.
CLAUDE_INPUT_USD = { "fable" => 10.0, "opus" => 5.0, "sonnet" => 2.0, "haiku" => 1.0 }.freeze
WRITE_1H = 2.00
WRITE_5M = 1.25

def rewarm_usd(model_id, ttl, tokens)
  family = CLAUDE_INPUT_USD.keys.find { |name| model_id.to_s.downcase.include?(name) }
  return nil if family.nil? || tokens.to_i.zero?

  read = model_id.to_s.downcase.include?("fable-5-1") ? 0.025 : 0.10
  write = ttl == "5m" ? WRITE_5M : WRITE_1H
  tokens.to_i / 1_000_000.0 * CLAUDE_INPUT_USD[family] * (write - read)
end

# Above the stdin read so --rates works without a payload.
if ARGV.include?("--rates")
  probes = %w[claude-opus-5 claude-opus-4-8 claude-fable-5 claude-fable-5-1
              claude-sonnet-5 claude-haiku-4-5]
  card = probes.to_h do |m|
    [m, { "1h" => rewarm_usd(m, "1h", 1_000_000), "5m" => rewarm_usd(m, "5m", 1_000_000) }]
  end
  puts JSON.generate(card)
  exit 0
end

# Countdown to a rate-limit reset, as the two largest useful units: "6d16h",
# "1h29m", "29m". Both windows share this so they always read the same way.
def reset_in(secs)
  secs = 0 if secs.negative?
  days, rest = secs.divmod(86_400)
  hours, rest = rest.divmod(3_600)
  mins = rest / 60

  return "#{days}d#{'%02d' % hours}h" if days.positive?
  return "#{hours}h#{'%02d' % mins}m" if hours.positive?
  "#{mins}m"
end

data = JSON.parse($stdin.read)
# File.write("/tmp/statusline-debug.json", JSON.pretty_generate(data))

model = data.dig("model", "display_name") || "?"
left = (data.dig("context_window", "remaining_percentage") || 100).to_i

now = Time.now.to_i

parts = ["#{CYAN}#{model}#{RESET}", "#{CYAN}ctx #{left}%#{RESET}"]

if (rate_limits = data["rate_limits"])
  five = rate_limits.fetch("five_hour")
  seven = rate_limits.fetch("seven_day")

  five_left = (100 - five.fetch("used_percentage")).round
  seven_left = (100 - seven.fetch("used_percentage")).round

  rate = "5h #{five_left}% ↻#{reset_in(five.fetch('resets_at') - now)}" \
    " · 7d #{seven_left}% ↻#{reset_in(seven.fetch('resets_at') - now)}"
  parts << "#{CYAN}#{rate}#{RESET}"
end

# --- Per-model weekly windows (the "Fable" meter) ----------------------------
#
# REMOVE WHEN NATIVE: Claude Code does not hand the per-model weekly bucket to
# the status line. Its payload builder assembles rate_limits from five_hour,
# seven_day and (gateway installs only) spend_limit, so `data` above can never
# carry it -- the bar /usage draws comes straight from GET /api/oauth/usage.
# The SDK already models it as rate_limits.model_scoped[] => {display_name,
# utilization, resets_at}; the day that field also reaches the status line
# payload, delete this whole section and read it off `data` like the windows
# above. Last checked against Claude Code 2.1.258.
#
# Nothing here ever blocks the render: a status line repaints far too often to
# afford a network round trip, so we serve whatever is cached and fork a
# detached refresh once the cache goes stale.

MODEL_WINDOW_TTL = 60
MODEL_WINDOW_CACHE = File.join(
  ENV["XDG_CACHE_HOME"] || File.join(Dir.home, ".cache"),
  "claude-statusline", "model-windows.json"
)

# The usage endpoint answers with a flat limits[] array; the per-model weekly
# buckets are its "weekly_scoped" entries, whose `percent` is already 0-100
# used. Their resets_at tracks the weekly window, so the 7d countdown printed
# above speaks for these too and we leave the clock off.
def fetch_model_windows
  token = JSON.parse(File.read(File.join(Dir.home, ".claude", ".credentials.json")))
              .dig("claudeAiOauth", "accessToken")
  return nil if token.nil? || token.empty?

  uri = URI("https://api.anthropic.com/api/oauth/usage")
  res = Net::HTTP.start(uri.host, uri.port, use_ssl: true,
                        open_timeout: 5, read_timeout: 8) do |http|
    http.get(uri.path,
             "Authorization" => "Bearer #{token}",
             "anthropic-beta" => "oauth-2025-04-20",
             "Content-Type" => "application/json")
  end
  return nil unless res.is_a?(Net::HTTPSuccess)

  JSON.parse(res.body).fetch("limits", []).filter_map do |limit|
    next unless limit["kind"] == "weekly_scoped"

    name = limit.dig("scope", "model", "display_name") or next
    { "name" => name.downcase, "used" => limit["percent"].to_f }
  end
end

def read_model_windows
  JSON.parse(File.read(MODEL_WINDOW_CACHE))
rescue StandardError
  nil
end

def write_model_windows(windows)
  FileUtils.mkdir_p(File.dirname(MODEL_WINDOW_CACHE))
  tmp = "#{MODEL_WINDOW_CACHE}.#{Process.pid}"
  File.write(tmp, JSON.generate("fetched_at" => Time.now.to_i, "windows" => windows))
  File.rename(tmp, MODEL_WINDOW_CACHE) # atomic: a concurrent render never reads a torn file
end

# The lock keeps every open session from firing the same request at once. A
# fetch that fails still stamps the cache -- keeping the last good numbers on
# screen -- so an expired token or an offline box costs one attempt per TTL
# rather than one per repaint.
def refresh_model_windows(previous)
  pid = fork do
    $stdout.reopen(File::NULL, "w")
    $stderr.reopen(File::NULL, "w")

    FileUtils.mkdir_p(File.dirname(MODEL_WINDOW_CACHE))
    lock = File.open("#{MODEL_WINDOW_CACHE}.lock", File::CREAT | File::RDWR, 0o600)
    exit unless lock.flock(File::LOCK_EX | File::LOCK_NB)

    fresh = begin
      fetch_model_windows
    rescue StandardError
      nil
    end
    write_model_windows(fresh || previous)
  end
  Process.detach(pid)
end

begin
  cache = read_model_windows
  windows = cache && cache["windows"]

  if cache.nil? || now - cache["fetched_at"].to_i >= MODEL_WINDOW_TTL
    refresh_model_windows(windows)
  end

  if windows && !windows.empty?
    meters = windows.map { |w| "#{w['name']} #{(100 - w['used'].to_f).round}%" }
    parts << "#{CYAN}#{meters.join(' · ')}#{RESET}"
  end
rescue StandardError
  # no token, no network, unreadable cache: just drop the segment
end

puts parts.join(" | ")

# --- Line 2: real-API-price spend + token stats ------------------------------
#
# Cost comes from Claude Code itself (`cost.total_cost_usd`), which prices the
# session at published per-model API rates. Token totals are summed from the
# transcript: one API response is written as several lines (one per content
# block) that each repeat the same `usage`, so dedupe on the message id.

def fmt(n)
  return n.to_s if n < 1_000
  return "%.1fk" % (n / 1_000.0) if n < 1_000_000
  "%.1fM" % (n / 1_000_000.0)
end

def token_totals(path, since = nil)
  return nil unless path && File.readable?(path)

  seen = {}
  input = 0
  output = 0
  cached = 0
  since_miss = 0

  File.foreach(path) do |line|
    next unless line.include?('"usage"')

    obj = (JSON.parse(line) rescue nil) or next
    next if obj["isSidechain"]

    msg = obj["message"] or next
    usage = msg["usage"] or next
    id = msg["id"] or next
    next if seen[id]

    seen[id] = true
    read = usage["cache_read_input_tokens"].to_i
    input += usage["input_tokens"].to_i + usage["cache_creation_input_tokens"].to_i + read
    output += usage["output_tokens"].to_i
    cached += read

    # Turns since the last rebuild: Claude Code counts misses, not the clean
    # run after one.
    if since && (ts = obj["timestamp"])
      since_miss += 1 if (Time.parse(ts).to_i rescue 0) > since
    end
  end

  { input: input, output: output, cached: cached, since_miss: since_miss }
end

# --- The prompt cache, as Claude Code itself measures it ---------------------
#
# `data["prompt_cache"]` needs 2.1.251+ and already excludes subagents. The
# deadline is where the cache dies *if you stop now*: any request, a read
# included, restarts the TTL for free, and it runs from the request's start.


def cache_segment(cache, since_miss, model_id)
  return nil if cache.nil? || !cache["caching_observed"]

  bits = []

  if (ratio = cache["hit_ratio"])
    pct = (ratio * 100).round
    colour = pct >= 90 ? GREEN : pct >= 70 ? AMBER : RED
    bits << "#{colour}cache #{pct}%#{RESET}"
  end

  ttl = cache["ttl"]
  risk = rewarm_usd(model_id, ttl, cache["recache_tokens_if_cold"])
  stake = risk ? " #{'%.2f' % risk} at stake" : ""

  if cache["warm"] && (expires = cache["expires_at"])
    left = expires.to_i - Time.now.to_i
    # Remaining time always, not just near the end: the glance before you walk
    # away is the one that needs it.
    colour = left < 600 ? AMBER : DIM
    bits << "#{colour}cold #{Time.at(expires.to_i).strftime('%H:%M')}" \
            " (#{reset_in([left, 0].max)}) · #{ttl}#{RESET}"
    bits << "#{DIM}$#{'%.2f' % risk} at stake#{RESET}" if risk
  else
    bits << "#{RED}COLD#{RESET}"
    bits << "#{RED}$#{'%.2f' % risk} to re-warm#{RESET}" if risk
  end

  misses = cache["misses"].to_i
  rebuilds = cache["expected_rebuilds"].to_i
  bits << "#{AMBER}#{misses} miss#{RESET}" if misses.positive?
  # "expected" -- compaction and tool-result clearing, not your doing.
  bits << "#{DIM}#{rebuilds} expected#{RESET}" if rebuilds.positive?
  bits << "#{DIM}#{since_miss} clean#{RESET}" if since_miss.positive?

  bits.join("#{DIM} · #{RESET}")
end

begin
  second = []

  if (usd = data.dig("cost", "total_cost_usd"))
    colour = usd < 5 ? GREEN : usd < 20 ? AMBER : RED
    second << "#{colour}$#{'%.2f' % usd}#{RESET}"
  end

  cache = data["prompt_cache"]
  totals = token_totals(data["transcript_path"], cache && cache["last_miss_at"])

  if totals && totals[:input] + totals[:output] > 0
    second << "#{DIM}in #{fmt(totals[:input])} · out #{fmt(totals[:output])}#{RESET}"
  end

  if (segment = cache_segment(cache, totals ? totals[:since_miss] : 0,
                              data.dig("model", "id")))
    second << segment
  elsif totals && totals[:input].positive?
    # Pre-2.1.251, or a provider that reports no cache tokens: fall back to the
    # ratio the transcript can still show.
    second << "#{DIM}cache #{(100.0 * totals[:cached] / totals[:input]).round}%#{RESET}"
  end

  puts second.join("#{DIM} · #{RESET}") unless second.empty?
rescue StandardError
  # never let the stats line break the status line
end
