#!/usr/bin/env ruby
# Run: ruby tests/sandbox_share_test.rb

require "minitest/autorun"
require "tmpdir"
require_relative "../sandbox-lib"

class SandboxShareTest < Minitest::Test
  include SandboxLib

  def setup
    @root = Dir.mktmpdir("sandbox-share-")
    @store = File.join(@root, "shares")
    @project = File.join(@root, "code", "app")
    FileUtils.mkdir_p(@project)
  end

  def teardown
    FileUtils.remove_entry(@root)
  end

  def share_args(*specs) = SandboxLib.share_args(@project, specs, store: @store)

  def test_a_share_lives_outside_the_project_named_after_it
    source = SandboxLib.share_source(@project, "logs", store: @store)
    assert_equal File.join(@store, @project.delete_prefix("/").tr("/", "-"), "logs"), source
  end

  def test_a_read_only_share_is_mounted_in_the_project
    dest = File.join(@project, ".sandbox-shares", "logs")
    assert_equal ["--ro-bind", SandboxLib.share_source(@project, "logs", store: @store), dest], share_args("logs:ro")
    assert File.directory?(dest)
  end

  def test_a_writable_share
    assert_equal "--bind", share_args("inbox:rw").first
  end

  def test_the_host_side_is_created
    share_args("logs:ro")
    assert File.directory?(SandboxLib.share_source(@project, "logs", store: @store))
  end

  def test_a_name_cannot_leave_its_folder
    assert_raises(ArgumentError) { share_args("../x:ro") }
    assert_raises(ArgumentError) { share_args("a/b:ro") }
  end

  def test_the_mode_is_ro_or_rw
    assert_raises(ArgumentError) { share_args("logs") }
    assert_raises(ArgumentError) { share_args("logs:dev") }
  end

  # The project is the agent's: it may have put a link where the share goes.
  def test_a_symlink_in_the_way_is_refused
    File.symlink(@root, File.join(@project, ".sandbox-shares"))
    assert_raises(ArgumentError) { share_args("logs:ro") }
  end

  # One share is one folder: never a project's whole set, or every project's.
  def test_only_single_shares_may_be_mounted
    base = ["--clearenv", "--unshare-net"]
    project_folder = File.dirname(SandboxLib.share_source(@project, "logs", store: @store))
    ok = base + share_args("logs:rw")
    assert_empty SandboxLib.invariant_violations(ok, home: @root, uid: 1000, share_store: @store)
    refute_empty SandboxLib.invariant_violations(base + ["--bind", project_folder, "/x"], home: @root, uid: 1000, share_store: @store)
    refute_empty SandboxLib.invariant_violations(base + ["--ro-bind", @store, "/x"], home: @root, uid: 1000, share_store: @store)
  end

  def test_a_symlinked_share_folder_is_refused
    FileUtils.mkdir_p(File.join(@project, ".sandbox-shares"))
    File.symlink(@root, File.join(@project, ".sandbox-shares", "logs"))
    assert_raises(ArgumentError) { share_args("logs:ro") }
  end
end
