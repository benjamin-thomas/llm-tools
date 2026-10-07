#!/usr/bin/env ruby
# Run: ruby tests/sandbox_git_test.rb

require "minitest/autorun"
require "tmpdir"
require_relative "../sandbox-lib"

class SandboxGitTest < Minitest::Test
  include SandboxLib

  def setup
    @root = Dir.mktmpdir("sandbox-git-")
    @main = File.join(@root, "repo")
    git("init", "-q", @main)
    git("-C", @main, "commit", "-q", "--allow-empty", "-m", "init")
    @common = File.join(@main, ".git")
  end

  def teardown
    FileUtils.remove_entry(@root)
  end

  def git(*args)
    system("git", "-c", "user.name=t", "-c", "user.email=t@t", *args, exception: true)
  end

  def add_worktree(name)
    path = File.join(@root, name)
    git("-C", @main, "worktree", "add", "-q", "-b", name, path)
    path
  end

  def args_for(project, writable:)
    args = rw(project)
    SandboxLib.bind_git_metadata!(args, project, writable: writable)
    args
  end

  # The flag of the last bind covering `path`: bwrap's last mount wins.
  def mode(args, path) = SandboxLib.effective_bind(args, path)

  def test_read_only_git_stays_read_only
    args = args_for(@main, writable: false)
    assert_equal "--ro-bind", mode(args, File.join(@common, "objects"))
    assert_equal "--ro-bind", mode(args, File.join(@common, "refs"))
  end

  def test_writable_git_can_commit
    args = args_for(@main, writable: true)
    assert_equal "--bind", mode(args, File.join(@common, "objects"))
    assert_equal "--bind", mode(args, File.join(@common, "refs/heads"))
    assert_equal "--bind", mode(args, File.join(@common, "index"))
  end

  # Writable config or hooks run the agent's code on the host, the next time
  # the user runs git there (core.fsmonitor, a pre-commit hook).
  def test_writable_git_keeps_config_and_hooks_read_only
    args = args_for(@main, writable: true)
    %w[config config.worktree hooks info modules worktrees].each do |entry|
      assert_equal "--ro-bind", mode(args, File.join(@common, entry)), entry
    end
  end

  def test_a_worktree_writes_its_own_metadata_only
    mine = add_worktree("mine")
    other = add_worktree("other")
    args = args_for(mine, writable: true)
    own_dir = File.join(@common, "worktrees", "mine")
    assert_equal "--bind", mode(args, File.join(own_dir, "HEAD"))
    assert_equal "--bind", mode(args, File.join(own_dir, "index"))
    assert_equal "--ro-bind", mode(args, File.join(own_dir, "config.worktree"))
    assert_equal "--ro-bind", mode(args, File.join(@common, "worktrees", "other", "HEAD"))
    assert_equal "--ro-bind", mode(args, File.join(@common, "config"))
    refute_nil other
  end

  # The .git file of a worktree says where its metadata lives: pointed at a
  # directory the agent wrote, it brings that directory's config along.
  def test_a_worktree_git_file_is_read_only
    mine = add_worktree("mine")
    args = args_for(mine, writable: true)
    assert_equal "--ro-bind", mode(args, File.join(mine, ".git"))
  end

  # A linked worktree's own metadata dir holds no config, hooks or info of its
  # own: git reads only its config.worktree there.
  def test_a_protected_worktree_passes_with_both_of_its_git_dirs
    mine = add_worktree("mine")
    own_dir = File.join(@common, "worktrees", "mine")
    args = ["--clearenv", "--unshare-net", *args_for(mine, writable: true)]
    assert_empty SandboxLib.invariant_violations(args, home: @root, uid: 1000, git_dirs: [@common, own_dir])
  end

  def test_a_writable_worktree_config_is_a_violation
    mine = add_worktree("mine")
    own_dir = File.join(@common, "worktrees", "mine")
    args = ["--clearenv", "--unshare-net", *args_for(mine, writable: true), *rw(File.join(own_dir, "config.worktree"))]
    refute_empty SandboxLib.invariant_violations(args, home: @root, uid: 1000, git_dirs: [@common, own_dir])
  end

  def test_unprotected_writable_git_is_a_violation
    args = ["--clearenv", "--unshare-net", *rw(@main)]
    violations = SandboxLib.invariant_violations(args, home: @root, uid: 1000, git_dirs: [@common])
    assert_match(/config/, violations.join("\n"))
  end

  def test_protected_writable_git_passes
    args = ["--clearenv", "--unshare-net", *args_for(@main, writable: true)]
    assert_empty SandboxLib.invariant_violations(args, home: @root, uid: 1000, git_dirs: [@common])
  end

  # A later mount of a parent directory undoes the protection beneath it.
  def test_a_later_parent_mount_is_a_violation
    args = ["--clearenv", "--unshare-net", *args_for(@main, writable: true), *rw(@root)]
    refute_empty SandboxLib.invariant_violations(args, home: @root, uid: 1000, git_dirs: [@common])
  end

  # A repository found among the mounts is checked even when nobody named it.
  def test_a_mounted_repository_is_found
    args = ["--clearenv", "--unshare-net", *rw(@main)]
    refute_empty SandboxLib.invariant_violations(args, home: @root, uid: 1000)
  end
end
