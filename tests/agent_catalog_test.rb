#!/usr/bin/ruby
# Run: /usr/bin/ruby tests/agent_catalog_test.rb

require "minitest/autorun"
require_relative "../agent-lib"

# What a host answers: the commands installed, each listing's output, the files.
class FakeHost
  def initialize(installed: [], outputs: {}, files: {})
    @installed = installed
    @outputs = outputs
    @files = files
  end

  def installed?(command) = @installed.include?(command)
  def capture(*command, input: nil) = @outputs.fetch(command, "")
  def read(path) = @files[path]
end

CODEX_MODELS = <<~JSON
  {"models": [
    {"slug": "gpt-6-luna", "visibility": "list",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "xhigh"}]},
    {"slug": "gpt-reserve", "visibility": "hide",
     "supported_reasoning_levels": [{"effort": "low"}]}
  ]}
JSON

GROK_MODELS = <<~TEXT
  You are logged in with grok.com.

  Default model: grok-4.7

  Available models:
    * grok-4.7 (default)
    - grok-4.5
TEXT

# What `pi --mode rpc` answers get_available_models with, past its first lines.
PI_MODELS = [
  { "type" => "extension_ui_request" },
  { "type" => "response", "command" => "get_available_models", "data" => { "models" => [
    { "provider" => "hyper", "id" => "glm-5.3", "reasoning" => true,
      "thinkingLevelMap" => { "off" => nil, "minimal" => nil, "low" => "low", "medium" => nil, "high" => "high",
                              "xhigh" => nil, "max" => "max" } },
    { "provider" => "hyper", "id" => "minimax-m2.7", "reasoning" => false },
    { "provider" => "mistral", "id" => "mistral-large-4", "reasoning" => true,
      "thinkingLevelMap" => { "off" => "none", "minimal" => nil, "low" => nil, "medium" => nil, "high" => "high",
                              "xhigh" => nil, "max" => nil } },
    { "provider" => "hyper", "id" => "plain", "reasoning" => true }
  ] } }
].map { |message| JSON.generate(message) }.join("\n")
PI_RPC = %w[pi --mode rpc --no-session].freeze

class AgentCatalogTest < Minitest::Test
  def lines(host) = AgentCatalog.lines(host)

  def specs(host) = lines(host).map { |line| line.split("\t").first }

  def test_claude_offers_its_models_at_each_effort
    host = FakeHost.new(installed: %w[claude])
    assert_includes specs(host), "claude:opus:high"
    assert_includes specs(host), "claude:haiku:low"
    assert_includes lines(host),
                    "claude:fable:max\t/clear\tenv CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false " \
                    "claude --dangerously-skip-permissions --model fable --effort max"
  end

  def test_a_harness_not_installed_offers_nothing
    assert_empty lines(FakeHost.new)
  end

  def test_codex_offers_its_listed_models_at_their_own_efforts
    host = FakeHost.new(installed: %w[codex], outputs: { %w[codex debug models] => CODEX_MODELS })
    assert_equal ["codex:gpt-6-luna:low", "codex:gpt-6-luna:xhigh"], specs(host)
    assert_includes lines(host),
                    "codex:gpt-6-luna:xhigh\t/clear\tcodex --dangerously-bypass-approvals-and-sandbox " \
                    "-m gpt-6-luna -c model_reasoning_effort=xhigh -c check_for_update_on_startup=false"
  end

  def test_codex_offers_nothing_when_its_listing_fails
    host = FakeHost.new(installed: %w[codex], outputs: { %w[codex debug models] => "not json" })
    assert_empty lines(host)
  end

  def test_grok_offers_its_models_at_the_efforts_each_takes
    host = FakeHost.new(installed: %w[grok], outputs: { %w[grok models] => GROK_MODELS })
    assert_equal %w[grok:grok-4.7:low grok:grok-4.7:medium grok:grok-4.7:high grok:grok-4.7:xhigh
                    grok:grok-4.5:low grok:grok-4.5:medium grok:grok-4.5:high], specs(host)
    assert_includes lines(host), "grok:grok-4.5:high\t/clear\tgrok --yolo -m grok-4.5 --reasoning-effort high"
  end

  # Installed, still not offered: see HARNESSES.
  def test_vibe_and_opencode_are_not_offered
    host = FakeHost.new(installed: %w[vibe opencode],
                        outputs: { %w[opencode models --verbose] => "hyper/glm-5.3\n{\"capabilities\": {\"reasoning\": true, \"toolcall\": true, \"output\": {\"text\": true}}\n}\n" },
                        files: { File.join(Dir.home, ".vibe", "config.toml") => "active_model = \"glm-5-3\"\n" })
    assert_empty lines(host)
  end

  def pi_host(settings = nil)
    files = settings ? { File.join(Dir.home, ".pi", "agent", "settings.json") => JSON.generate(settings) } : {}
    FakeHost.new(installed: %w[pi], outputs: { PI_RPC => PI_MODELS }, files: files)
  end

  def test_pi_without_scoped_models_offers_each_model_that_thinks
    assert_equal %w[hyper/glm-5.3 mistral/mistral-large-4 hyper/plain],
                 specs(pi_host).map { |spec| spec.split(":")[1] }.uniq
    assert_includes lines(pi_host), "pi:hyper/glm-5.3:high\t/new\tpi --model hyper/glm-5.3 --thinking high"
  end

  # As pi's own picker: a level its map sets to null is out, xhigh and max
  # only when it maps them.
  def test_pi_offers_each_model_the_thinking_levels_pi_lets_it_take
    efforts = lambda do |model|
      specs(pi_host).select { |spec| spec.start_with?("pi:#{model}:") }.map { |spec| spec.split(":").last }
    end
    assert_equal %w[low high max], efforts.call("hyper/glm-5.3")
    assert_equal %w[off high], efforts.call("mistral/mistral-large-4")
    assert_equal %w[off minimal low medium high], efforts.call("hyper/plain")
  end

  # Its scoped models, those its Ctrl+P cycles through, are the ones you chose.
  def test_pi_offers_its_scoped_models_in_their_order_thinking_or_not
    host = pi_host("enabledModels" => %w[hyper/minimax-m2.7 hyper/glm-5.3 hyper/gone])
    assert_equal %w[hyper/minimax-m2.7 hyper/glm-5.3], specs(host).map { |spec| spec.split(":")[1] }.uniq
    assert_includes specs(host), "pi:hyper/minimax-m2.7:off"
    refute_includes specs(host), "pi:hyper/minimax-m2.7:high"
  end

  def test_pi_scoped_models_may_be_globs
    host = pi_host("enabledModels" => %w[hyper/*-m2*])
    assert_equal %w[hyper/minimax-m2.7], specs(host).map { |spec| spec.split(":")[1] }.uniq
  end

  def test_pi_reads_its_settings_under_its_agent_dir
    ENV["PI_CODING_AGENT_DIR"] = "/elsewhere"
    host = FakeHost.new(installed: %w[pi], outputs: { PI_RPC => PI_MODELS },
                        files: { "/elsewhere/settings.json" => JSON.generate("enabledModels" => %w[hyper/minimax-m2.7]) })
    assert_equal %w[pi:hyper/minimax-m2.7:off], specs(host)
  ensure
    ENV.delete("PI_CODING_AGENT_DIR")
  end

  def test_a_model_a_spec_cannot_hold_is_skipped
    host = FakeHost.new(installed: %w[grok], outputs: { %w[grok models] => "  * odd:name\n  * fine\n" })
    assert_equal %w[fine], specs(host).map { |spec| spec.split(":")[1] }.uniq
  end

  def test_the_command_line_is_quoted_for_a_shell
    host = FakeHost.new(installed: %w[grok], outputs: { %w[grok models] => "  * grok-5[1m]\n" })
    assert_includes lines(host).first, "-m grok-5\\[1m\\]"
  end

  def test_harnesses_come_in_a_fixed_order
    host = FakeHost.new(installed: %w[pi claude], outputs: { PI_RPC => PI_MODELS })
    assert_equal %w[claude pi], specs(host).map { |spec| spec.split(":").first }.uniq
  end
end
