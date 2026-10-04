#!/usr/bin/env ruby
# Run: ruby tests/sandbox_approval_test.rb

require "minitest/autorun"
require "stringio"
require "tmpdir"
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

# The store: ~/.sandbox-agent/approvals/<project path, dashed>/<sha256>, one
# file per approved version. Exercised on a throwaway store and project.
class ApprovalStoreTest < Minitest::Test
  def setup
    @store = Dir.mktmpdir("approvals-")
    @root = Dir.mktmpdir("projects-")
    @project = File.join(@root, "code", "erp-workspace")
    FileUtils.mkdir_p(@project)
  end

  def teardown
    [@store, @root].each { |d| FileUtils.remove_entry(d) }
  end

  def write_mounts(dir, text) = File.write(File.join(dir, ".sandbox-mounts"), text)
  def approve(dir, answer = "y") = SandboxLib.approve_mounts!(dir, input: StringIO.new("#{answer}\n"), store: @store)
  def approved?(dir) = SandboxLib.mounts_approved?(dir, store: @store)

  def test_a_project_folder_is_named_after_its_path
    assert_equal File.join(@store, "home-me-code-github.com-erp-workspace"),
                 SandboxLib.project_approvals("/home/me/code/github.com/erp-workspace", store: @store)
  end

  def test_approving_keeps_the_text_under_its_hash
    write_mounts(@project, "net:a.org\n")
    capture_io { approve(@project) }
    files = Dir[File.join(SandboxLib.project_approvals(@project, store: @store), "*")]
    assert_equal [Digest::SHA256.hexdigest("net:a.org\n")], files.map { |f| File.basename(f) }
    assert approved?(@project)
  end

  def test_saying_no_approves_nothing
    write_mounts(@project, "net:a.org\n")
    capture_io { approve(@project, "n") }
    refute approved?(@project)
  end

  # A new worktree with the same file is already approved.
  def test_same_content_elsewhere_is_approved
    write_mounts(@project, "net:a.org\n")
    capture_io { approve(@project) }
    worktree = File.join(@root, "worktrees", "feature")
    FileUtils.mkdir_p(worktree)
    write_mounts(worktree, "net:a.org\n")
    assert approved?(worktree)
  end

  def test_the_diff_compares_with_the_last_approved_version
    write_mounts(@project, "net:a.org\n")
    capture_io { approve(@project) }
    sleep 0.01
    write_mounts(@project, "net:b.org\n")
    capture_io { approve(@project) }
    assert_equal "net:b.org\n", SandboxLib.approved_content(@project, store: @store)
  end

  # Only the last ten versions are kept: enough to switch back and forth
  # without asking again, not a growing pile.
  def test_keeps_the_ten_latest_versions
    12.times do |i|
      write_mounts(@project, "internet:v#{i}.org\n")
      capture_io { approve(@project) }
      folder = SandboxLib.project_approvals(@project, store: @store)
      Dir[File.join(folder, "*")].each { |f| File.utime(Time.now - 100 + i, Time.now - 100 + i, f) if File.read(f) == "internet:v#{i}.org\n" }
    end
    kept = Dir[File.join(SandboxLib.project_approvals(@project, store: @store), "*")].map { |f| File.read(f) }
    assert_equal (2..11).map { |i| "internet:v#{i}.org\n" }.sort, kept.sort
  end

  def test_unchanged_file_asks_nothing
    write_mounts(@project, "net:a.org\n")
    capture_io { approve(@project) }
    out, = capture_io { assert_equal 0, approve(@project, "n") }
    assert_match(/already approved/, out)
  end
end
