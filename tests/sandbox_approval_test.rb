#!/usr/bin/env ruby
# Run: ruby tests/sandbox_approval_test.rb

require "minitest/autorun"
require_relative "../sandbox-lib"

# What `sandbox-agent allow` shows before asking.
class ApprovalDiffTest < Minitest::Test
  def diff(previous, current, color: false) = SandboxLib.approval_diff(previous, current, color: color)

  def test_shows_what_changed_since_the_last_approval
    assert_equal ["- internet:a.org", "+ internet:b.org"], diff("ro:/x\ninternet:a.org\n", "ro:/x\ninternet:b.org\n")
  end

  def test_first_approval_shows_every_line_as_added
    assert_equal ["+ # docs", "+ internet:a.org"], diff(nil, "# docs\n\ninternet:a.org\n")
  end

  def test_removed_lines_are_red_and_added_ones_green
    red, green = diff("internet:a.org\n", "internet:b.org\n", color: true)
    assert_equal "\e[31m- internet:a.org\e[0m", red
    assert_equal "\e[32m+ internet:b.org\e[0m", green
  end

  def test_no_color_without_a_terminal
    refute_includes diff("internet:a.org\n", "internet:b.org\n").join, "\e["
  end
end
