#!/usr/bin/env ruby

# Symlink to ~/.grok/statusline.rb, then in ~/.grok/config.toml:
#   [ui.status_line]
#   type = "command"
#   command = "~/.grok/statusline.rb"
#   padding = 0
#   refresh_interval = 60

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

# $/MTok (input, cache-read, output) at the <200k and ≥200k rows, mirroring
# ../token_recap/native.py. Writes are free. The session `$` is this card
# applied to session_usage, not what SuperGrok actually bills.
# tests/test_grok_statusline.py guards the card.
GROK = {
  "grok-4.6" => { "lo" => [2.0, 0.50, 6.0], "hi" => [4.0, 1.00, 12.0] },
  "grok-4.5" => { "lo" => [2.0, 0.30, 6.0], "hi" => [4.0, 0.60, 12.0] },
  "grok-build-0.1" => { "lo" => [1.0, 0.20, 2.0], "hi" => [2.0, 0.40, 4.0] }
}.freeze
FALLBACK = "grok-4.6"
LONG_CONTEXT = 200_000
CLIFF_WARN = 180_000
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
ISSUER = "https://auth.x.ai"

def rates_for(model_id, prompt)
  key = GROK.key?(model_id.to_s) ? model_id.to_s : FALLBACK
  pair = GROK.fetch(key)
  prompt.to_i >= LONG_CONTEXT ? pair.fetch("hi") : pair.fetch("lo")
end

# List-price session total. The 200k cliff is keyed on the live window
# (context_tokens), not on cumulative session_input — that sum is many
# turns and would trip the high row even when no single prompt did.
def session_api_usd(model_id, prompt, uncached, cache_read, output)
  return nil if uncached.to_i + cache_read.to_i + output.to_i <= 0

  inp, cread, out = rates_for(model_id, prompt)
  uncached.to_i / 1_000_000.0 * inp +
    cache_read.to_i / 1_000_000.0 * cread +
    output.to_i / 1_000_000.0 * out
end

if ARGV.include?("--rates")
  card = GROK.keys.to_h do |model|
    lo = rates_for(model, 1)
    hi = rates_for(model, LONG_CONTEXT)
    pack = lambda do |row|
      { "input" => row[0], "cache_read" => row[1], "output" => row[2] }
    end
    [model, { "<200k" => pack.call(lo), ">=200k" => pack.call(hi) }]
  end
  puts JSON.generate(card)
  exit 0
end

# Countdown as the two largest useful units: "6d16h", "1h29m", "29m".
def reset_in(secs)
  secs = 0 if secs.negative?
  days, rest = secs.divmod(86_400)
  hours, rest = rest.divmod(3_600)
  mins = rest / 60

  return "#{days}d#{format('%02d', hours)}h" if days.positive?
  return "#{hours}h#{format('%02d', mins)}m" if hours.positive?

  "#{mins}m"
end

def fmt(n)
  n = n.to_i
  return n.to_s if n < 1_000

  if n < 1_000_000
    q = n / 1_000.0
    return (q % 1).zero? ? "#{q.to_i}k" : format("%.1fk", q)
  end

  q = n / 1_000_000.0
  (q % 1).zero? ? "#{q.to_i}M" : format("%.1fM", q)
end

def cache_path
  ENV["GROK_STATUSLINE_CACHE"] || File.join(
    ENV["XDG_CACHE_HOME"] || File.join(Dir.home, ".cache"),
    "grok-statusline", "billing.json"
  )
end

def auth_path
  ENV["GROK_STATUSLINE_AUTH"] || File.join(Dir.home, ".grok", "auth.json")
end

def log_path
  ENV["GROK_STATUSLINE_LOG"] || File.join(Dir.home, ".grok", "logs", "unified.jsonl")
end

def read_cache
  blob = JSON.parse(File.read(cache_path))
  blob.is_a?(Hash) ? blob : nil
rescue StandardError
  nil
end

def write_cache(blob)
  FileUtils.mkdir_p(File.dirname(cache_path))
  tmp = "#{cache_path}.#{Process.pid}"
  File.write(tmp, JSON.generate(blob))
  File.rename(tmp, cache_path)
end

def usable?(cache, now)
  return false unless cache.is_a?(Hash)
  return false if cache["failed"]
  return false unless cache["used_percent"].is_a?(Numeric)

  cache["resets_at"].to_i > now
end

# Grok kills leftover children when the run ends, so we never fork. Network
# only on a missing cache (first paint) or trigger=refresh_interval. A busy
# turn re-runs this continuously; those state paints must read the file.
def should_fetch?(trigger, cache)
  cache.nil? || trigger == "refresh_interval"
end

def with_lock
  FileUtils.mkdir_p(File.dirname(cache_path))
  lock = File.open("#{cache_path}.lock", File::CREAT | File::RDWR, 0o600)
  begin
    yield if lock.flock(File::LOCK_EX | File::LOCK_NB)
  ensure
    lock.close
  end
end

def parse_billing(blob, now)
  return nil unless blob.is_a?(Hash)

  config = blob["config"]
  if config.nil? && blob["ctx"].is_a?(Hash)
    config = blob["ctx"]["config"]
  end
  return nil unless config.is_a?(Hash)

  used = config["creditUsagePercent"]
  period = config["currentPeriod"]
  return nil unless used.is_a?(Numeric) && period.is_a?(Hash)
  return nil unless period["end"].is_a?(String)

  resets = Time.parse(period["end"]).to_i
  return nil if resets <= now

  { "used_percent" => used.to_f, "resets_at" => resets }
rescue StandardError
  nil
end

def oidc_token
  blob = JSON.parse(File.read(auth_path))
  return nil unless blob.is_a?(Hash)

  blob.each do |name, entry|
    next unless name.to_s.start_with?(ISSUER)
    next unless entry.is_a?(Hash) && entry["auth_mode"] == "oidc"

    key = entry["key"]
    return key if key.is_a?(String) && !key.empty?
  end
  nil
rescue StandardError
  nil
end

def try_live(now)
  token = oidc_token
  return nil if token.nil?

  uri = URI(BILLING_URL)
  res = Net::HTTP.start(uri.host, uri.port, use_ssl: true,
                        open_timeout: 2, read_timeout: 3) do |http|
    http.get(uri.request_uri,
             "Authorization" => "Bearer #{token}",
             "Accept" => "application/json",
             "User-Agent" => "grok-statusline")
  end
  return nil unless res.is_a?(Net::HTTPSuccess)

  parse_billing(JSON.parse(res.body), now)
rescue StandardError
  nil
end

def try_log(now)
  path = log_path
  return nil unless File.readable?(path)

  size = File.size(path)
  File.open(path, "rb") do |file|
    drop_torn = size > 524_288
    file.seek([size - 524_288, 0].max)
    data = file.read.to_s
    data = data.split("\n", 2)[1].to_s if drop_torn
    data.each_line.reverse_each do |line|
      next unless line.include?("billing: fetched credits config")

      obj = begin
        JSON.parse(line)
      rescue JSON::ParserError
        next
      end
      next unless obj.is_a?(Hash) && obj["msg"] == "billing: fetched credits config"

      parsed = parse_billing(obj, now)
      return parsed if parsed
    end
  end
  nil
rescue StandardError
  nil
end

def load_billing(trigger, now)
  cache = read_cache
  return cache if usable?(cache, now) && !should_fetch?(trigger, cache)

  result = cache
  with_lock do
    cache = read_cache
    if should_fetch?(trigger, cache)
      fresh = try_live(now) || try_log(now)
      if usable?(fresh, now)
        write_cache(fresh.merge("fetched_at" => now))
        result = fresh
      elsif usable?(cache, now)
        result = cache
      else
        write_cache("fetched_at" => now, "failed" => true) if cache.nil?
        result = cache
      end
    else
      result = cache
    end
  end
  usable?(result, now) ? result : nil
end

begin
  data = JSON.parse($stdin.read)
rescue JSON::ParserError
  exit 0
end
exit 0 unless data.is_a?(Hash)

now = (ENV["GROK_STATUSLINE_NOW"] || Time.now.to_i).to_i
model_id = data.dig("model", "id")
model = data.dig("model", "display_name") || model_id || "?"
ctx = data["context_window"] || {}
billing = load_billing(data["trigger"].to_s, now)

parts = ["#{CYAN}#{model}#{RESET}"]
effort = data.dig("effort", "level")
parts << "#{DIM}#{effort}#{RESET}" if effort.is_a?(String) && !effort.empty?

remaining = ctx["remaining_percentage"]
unless remaining.nil?
  remaining = remaining.to_i
  threshold = ctx["auto_compact_threshold_percent"]
  danger = threshold ? (100 - threshold.to_i) : 20
  colour = if remaining <= 5
             RED
           elsif remaining <= danger
             AMBER
           else
             CYAN
           end
  parts << "#{colour}ctx #{remaining}%#{RESET}"
end

if billing
  left = (100 - billing["used_percent"]).round
  colour = if left <= 10
             RED
           elsif left <= 30
             AMBER
           else
             CYAN
           end
  parts << "#{colour}7d #{left}% ↻#{reset_in(billing['resets_at'] - now)}#{RESET}"
end

usage = ctx["session_usage"] || {}
uncached = usage["input_tokens"]
cache_read = usage["cache_read_input_tokens"].to_i
billed_out = usage["output_tokens"] || ctx["session_output_tokens"]
usd = session_api_usd(model_id, ctx["context_tokens"], uncached, cache_read, billed_out)
usd = data.dig("cost", "total_cost_usd") if usd.nil?
if usd.is_a?(Numeric) && usd >= 0.005
  colour = usd < 5 ? GREEN : usd < 20 ? AMBER : RED
  parts << "#{colour}$#{format('%.2f', usd)}#{RESET}"
end

input = ctx["session_input_tokens"]
output = ctx["session_output_tokens"]
if input.to_i.positive? || output.to_i.positive?
  parts << "#{DIM}in #{fmt(input)} · out #{fmt(output)}#{RESET}"
end

if input.to_i.positive? && cache_read.positive?
  pct = (100.0 * cache_read / input.to_i).round
  colour = pct >= 90 ? GREEN : pct >= 70 ? AMBER : RED
  parts << "#{colour}cache #{pct}%#{RESET}"
end

tokens = ctx["context_tokens"].to_i
if tokens >= LONG_CONTEXT
  parts << "#{AMBER}≥200k 2×#{RESET}"
elsif tokens >= CLIFF_WARN
  parts << "#{AMBER}2× in #{fmt(LONG_CONTEXT - tokens)}#{RESET}"
end

puts parts.join(" | ")
