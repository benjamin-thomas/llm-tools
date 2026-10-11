#!/usr/bin/ruby
# Run: /usr/bin/ruby tests/agent_pick_test.rb

require "minitest/autorun"
require "stringio"
require "tmpdir"
require_relative "../agent-lib"

CATALOG = [
  "claude:opus:high\t/clear\tclaude --model opus --effort high",
  "claude:opus:max\t/clear\tclaude --model opus --effort max",
  "claude:sonnet:high\t/clear\tclaude --model sonnet --effort high",
  "pi:hyper/glm-5.3:high\t/new\tpi --model hyper/glm-5.3 --thinking high",
  "pi:hyper/glm-5.3:off\t/new\tpi --model hyper/glm-5.3 --thinking off"
].freeze

class AgentPickTest < Minitest::Test
  def setup
    @dir = Dir.mktmpdir("agent-pick-")
    @choices = File.join(@dir, "tmp", "choices")
    @asked = []
    @answers = []
    @err = StringIO.new
  end

  def teardown = FileUtils.rm_rf(@dir)

  def remember(text)
    FileUtils.mkdir_p(File.dirname(@choices))
    File.write(@choices, text)
  end

  # fzf answers the next of @answers, or the preselected (first) option.
  def fzf
    lambda do |header, options|
      @asked << [header, options]
      @answers.empty? ? options.first : @answers.shift
    end
  end

  def pick(roles, input: "", tty: true)
    AgentPick.new(catalog: CATALOG, choices: @choices, fzf: fzf, input: StringIO.new(input), err: @err, tty: tty)
             .lines(roles)
  end

  def test_without_a_terminal_it_keeps_the_remembered_agents
    remember("tab1=claude:opus:high\ntab2=pi:hyper/glm-5.3:off\n")
    assert_equal ["tab1\tclaude:opus:high\t/clear\tclaude --model opus --effort high",
                  "tab2\tpi:hyper/glm-5.3:off\t/new\tpi --model hyper/glm-5.3 --thinking off"],
                 pick(%w[tab1 tab2], tty: false)
    assert_empty @asked
  end

  def test_without_a_terminal_a_role_never_picked_fails
    remember("tab1=claude:opus:high\n")
    error = assert_raises(AgentPick::Failed) { pick(%w[tab1 tab2], tty: false) }
    assert_match(/tab2/, error.message)
  end

  def test_without_a_terminal_an_agent_gone_from_the_catalog_fails
    remember("tab1=claude:opus-old:high\n")
    assert_raises(AgentPick::Failed) { pick(%w[tab1], tty: false) }
  end

  def test_it_shows_the_remembered_agents_and_keeps_them_on_enter
    remember("tab1=claude:opus:high\ntab2=pi:hyper/glm-5.3:off\n")
    assert_equal 2, pick(%w[tab1 tab2], input: "\n").size
    assert_empty @asked
    assert_match(/Keep these agents\?/, @err.string)
    assert_match(/tab1\s+claude:opus:high/, @err.string)
    assert_match(/tab2\s+pi:hyper\/glm-5.3:off/, @err.string)
  end

  def test_a_closed_input_keeps_them_too
    remember("tab1=claude:opus:high\n")
    assert_equal 1, pick(%w[tab1], input: "").size
    assert_empty @asked
  end

  def test_no_picks_every_role_again_harness_then_model_then_effort
    remember("tab1=claude:opus:high\ntab2=pi:hyper/glm-5.3:off\n")
    @answers = %w[claude sonnet high pi hyper/glm-5.3 off]
    lines = pick(%w[tab1 tab2], input: "n\n")
    assert_equal ["tab1: harness", "tab1: claude model", "tab1: effort",
                  "tab2: harness", "tab2: pi model", "tab2: effort"], @asked.map(&:first)
    assert_equal "tab1\tclaude:sonnet:high\t/clear\tclaude --model sonnet --effort high", lines.first
  end

  def test_the_remembered_choice_comes_first_at_each_step
    remember("tab1=claude:opus:max\n")
    pick(%w[tab1], input: "n\n")
    assert_equal [%w[claude pi], %w[opus sonnet], %w[max high]], @asked.map(&:last)
  end

  def test_another_harness_lists_its_own_models_and_efforts
    remember("tab1=claude:opus:max\n")
    @answers = %w[pi]
    pick(%w[tab1], input: "n\n")
    assert_equal [%w[claude pi], %w[hyper/glm-5.3], %w[high off]], @asked.map(&:last)
  end

  def test_picks_are_remembered_beside_other_keys
    remember("other=kept\ntab1=claude:opus:high\n")
    @answers = %w[claude sonnet high]
    pick(%w[tab1], input: "n\n")
    assert_equal "other=kept\ntab1=claude:sonnet:high\n", File.read(@choices)
  end

  def test_a_role_never_picked_is_picked_without_asking_to_keep
    pick(%w[tab1])
    assert_equal ["tab1: harness", "tab1: claude model", "tab1: effort"], @asked.map(&:first)
    assert_equal "tab1=claude:opus:high\n", File.read(@choices)
    refute_match(/Keep/, @err.string)
  end

  def test_an_agent_gone_from_the_catalog_is_picked_again_its_harness_first
    remember("tab1=pi:hyper/gone:high\n")
    pick(%w[tab1])
    assert_equal %w[pi claude], @asked.first.last
  end

  def test_a_cancelled_pick_fails
    @answers = [nil]
    assert_raises(AgentPick::Failed) { pick(%w[tab1]) }
  end

  def test_a_role_is_a_lowercase_name
    assert_raises(AgentPick::Failed) { pick(["tab 1"]) }
    assert_raises(AgentPick::Failed) { pick([]) }
  end
end
