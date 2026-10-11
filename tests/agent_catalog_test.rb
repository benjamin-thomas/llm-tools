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
  def capture(*command) = @outputs.fetch(command, "")
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

VIBE_CONFIG = <<~TOML
  active_model = "glm-5-3"

  [[providers]]
  name = "mistral"

  [[models]]
  name = "mistral-vibe-cli-latest"
  provider = "mistral"
  alias = "mistral-medium-3.5"
  thinking = "high"

  [[models]]
  name = "devstral"
  alias = "local"
TOML

OPENCODE_MODELS = <<~TEXT
  hyper/glm-5.3
  {
    "id": "glm-5.3",
    "capabilities": {"reasoning": true, "toolcall": true, "output": {"text": true, "audio": false}},
    "variants": {}
  }
  mistral/mistral-medium-latest
  {
    "id": "mistral-medium-latest",
    "capabilities": {"reasoning": true, "toolcall": true, "output": {"text": true}},
    "variants": {"none": {"reasoningEffort": "none"}, "high": {"reasoningEffort": "high"}}
  }
  mistral/codestral-latest
  {
    "id": "codestral-latest",
    "capabilities": {"reasoning": false, "toolcall": true, "output": {"text": true}},
    "variants": {}
  }
  mistral/mistral-embed
  {
    "id": "mistral-embed",
    "capabilities": {"toolcall": false, "output": {"text": true}}
  }
  mistral/voxtral-mini-tts-latest
  {
    "id": "voxtral-mini-tts-latest",
    "capabilities": {"toolcall": false, "output": {"text": false, "audio": true}}
  }
TEXT

PI_MODELS = <<~TEXT
  provider  model          context  max-out  thinking  images
  hyper     glm-5.3        1M       128K     yes       no
  hyper     minimax-m2.7   262.1K   6.6K     no        no
TEXT

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

  def test_vibe_offers_its_active_model_and_aliases_its_config_sets_thinking_for
    host = FakeHost.new(installed: %w[vibe], files: { File.join(Dir.home, ".vibe", "config.toml") => VIBE_CONFIG })
    assert_equal %w[vibe:glm-5-3:default vibe:mistral-medium-3.5:default vibe:local:default], specs(host)
    assert_includes lines(host), "vibe:local:default\t/clear\tenv VIBE_ACTIVE_MODEL=local vibe --yolo"
  end

  def test_vibe_reads_its_config_under_vibe_home
    ENV["VIBE_HOME"] = "/elsewhere"
    host = FakeHost.new(installed: %w[vibe], files: { "/elsewhere/config.toml" => VIBE_CONFIG })
    assert_includes specs(host), "vibe:local:default"
  ensure
    ENV.delete("VIBE_HOME")
  end

  def test_opencode_offers_the_models_that_think_call_tools_and_write_text_at_their_variants
    host = FakeHost.new(installed: %w[opencode], outputs: { %w[opencode models --verbose] => OPENCODE_MODELS })
    assert_equal %w[opencode:hyper/glm-5.3:default opencode:mistral/mistral-medium-latest:none
                    opencode:mistral/mistral-medium-latest:high], specs(host)
    assert_includes lines(host),
                    "opencode:mistral/mistral-medium-latest:high\t/clear\topencode-variant mistral/mistral-medium-latest high"
  end

  # Its TUI takes no variant: it starts on the one it remembers per model.
  def test_opencode_variant_is_remembered_where_the_tui_reads_it
    Dir.mktmpdir do |dir|
      state = File.join(dir, "opencode", "model.json")
      FileUtils.mkdir_p(File.dirname(state))
      File.write(state, JSON.generate("recent" => [], "variant" => { "hyper/kimi-k3" => "max", "mistral/m" => "none" }))
      AgentCatalog.remember_opencode_variant(state, "mistral/m", "high")
      assert_equal({ "recent" => [], "variant" => { "hyper/kimi-k3" => "max", "mistral/m" => "high" } },
                   JSON.parse(File.read(state)))
    end
  end

  def test_opencode_default_variant_forgets_the_remembered_one
    Dir.mktmpdir do |dir|
      state = File.join(dir, "model.json")
      File.write(state, JSON.generate("variant" => { "hyper/glm-5.3" => "max" }))
      AgentCatalog.remember_opencode_variant(state, "hyper/glm-5.3", "default")
      assert_equal({ "variant" => {} }, JSON.parse(File.read(state)))
    end
  end

  def test_opencode_variant_starts_a_state_file_when_none_exists
    Dir.mktmpdir do |dir|
      state = File.join(dir, "opencode", "model.json")
      AgentCatalog.remember_opencode_variant(state, "mistral/m", "high")
      assert_equal({ "variant" => { "mistral/m" => "high" } }, JSON.parse(File.read(state)))
    end
  end

  def pi_host(settings = nil)
    files = settings ? { File.join(Dir.home, ".pi", "agent", "settings.json") => JSON.generate(settings) } : {}
    FakeHost.new(installed: %w[pi], outputs: { %w[pi --list-models] => PI_MODELS }, files: files)
  end

  def test_pi_without_scoped_models_offers_each_model_that_thinks_at_its_thinking_levels
    assert_equal %w[pi:hyper/glm-5.3:off pi:hyper/glm-5.3:minimal pi:hyper/glm-5.3:low pi:hyper/glm-5.3:medium
                    pi:hyper/glm-5.3:high pi:hyper/glm-5.3:xhigh pi:hyper/glm-5.3:max], specs(pi_host)
    assert_includes lines(pi_host), "pi:hyper/glm-5.3:high\t/new\tpi --model hyper/glm-5.3 --thinking high"
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
    host = FakeHost.new(installed: %w[pi], outputs: { %w[pi --list-models] => PI_MODELS },
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
    host = FakeHost.new(installed: %w[pi claude], outputs: { %w[pi --list-models] => PI_MODELS })
    assert_equal %w[claude pi], specs(host).map { |spec| spec.split(":").first }.uniq
  end
end
