#!/usr/bin/env ruby
# Run: ruby tests/sandbox_copy_test.rb

require "minitest/autorun"
require "stringio"
require "tmpdir"
require "fileutils"
require_relative "../sandbox-lib"

# build_base_args revokes the host's xhost rule first; a test has no business
# touching the host display.
def SandboxX11.close_host_display!; end

# `copy:<host path>`: the sandboxes' own copy of a host directory, following
# the host one generation at a time. Exercised on a throwaway home and store.
class SandboxCopyTest < Minitest::Test
  def setup
    @home = Dir.mktmpdir("sandbox-copy-home-")
    @store = SandboxLib.copy_store(@home)
    @host = File.join(@home, ".rbenv")
    FileUtils.mkdir_p(File.join(@host, "versions", "3.3", "bin"))
    File.write(File.join(@host, "versions", "3.3", "bin", "ruby"), "host ruby")
    @locks = []
  end

  def teardown
    @locks.each(&:close)
    FileUtils.chmod_R("u+w", @home)
    FileUtils.remove_entry(@home)
  end

  # A launch: the generation it binds, kept locked as a running sandbox would.
  def launch
    copy = SandboxLib.prepare_copy!(@host, store: @store)
    @locks << copy[:lock]
    copy
  end

  # A sandbox that exits.
  def exit_sandbox(copy)
    @locks.delete(copy[:lock])
    copy[:lock].close
  end

  # The host changed: something installed, something rewritten.
  def change_host
    File.write(File.join(@host, "versions", "3.3", "bin", "gem"), "host gem")
  end

  def test_a_copy_line_is_parsed_like_the_others
    Dir.mktmpdir("copy-project-") do |project|
      File.write(File.join(project, ".sandbox-mounts"), "copy:~/.rbenv  # its own\n")
      mount = SandboxLib.parse_sandbox_mounts(project, Dir.home).first
      assert_equal "copy", mount[:mode]
      assert_equal File.join(Dir.home, ".rbenv"), mount[:path]
    end
  end

  def test_the_copies_live_in_sandbox_agent_s_own_data_never_in_the_host_s_cache
    assert_equal File.join(@home, ".local", "share", "sandbox-agent", "copies"), @store
  end

  def test_a_first_launch_copies_the_host_tree
    copy = launch

    assert copy[:built]
    assert copy[:dir].start_with?(File.join(@store, ""))
    assert_equal "host ruby", File.read(File.join(copy[:dir], "versions", "3.3", "bin", "ruby"))
  end

  def test_the_copy_is_a_copy_never_a_link_to_the_host_s_files
    copy = launch

    host = File.stat(File.join(@host, "versions", "3.3", "bin", "ruby"))
    mine = File.stat(File.join(copy[:dir], "versions", "3.3", "bin", "ruby"))
    refute_equal host.ino, mine.ino
    File.write(File.join(copy[:dir], "versions", "3.3", "bin", "ruby"), "planted")
    assert_equal "host ruby", File.read(File.join(@host, "versions", "3.3", "bin", "ruby"))
  end

  def test_an_unchanged_host_keeps_the_copy_and_what_sandboxes_added_to_it
    first = launch
    File.write(File.join(first[:dir], "installed-inside"), "a gem")
    exit_sandbox(first)

    second = launch

    refute second[:built]
    assert_equal first[:dir], second[:dir]
    assert File.exist?(File.join(second[:dir], "installed-inside"))
  end

  def test_a_changed_host_gets_a_new_generation_with_its_changes
    first = launch
    exit_sandbox(first)
    change_host

    second = launch

    assert second[:built]
    refute_equal first[:dir], second[:dir]
    assert_equal "host gem", File.read(File.join(second[:dir], "versions", "3.3", "bin", "gem"))
  end

  def test_current_names_the_newest_generation
    launch
    change_host
    second = launch

    current = File.join(File.dirname(second[:dir]), "current")
    assert_equal File.basename(second[:dir]), File.readlink(current)
  end

  def test_a_generation_nobody_uses_is_deleted
    first = launch
    exit_sandbox(first)
    change_host

    launch

    refute File.exist?(first[:dir])
    refute File.exist?("#{first[:dir]}.lock")
  end

  def test_a_running_sandbox_keeps_its_generation_untouched
    running = launch
    File.write(File.join(running[:dir], "installed-inside"), "a gem")
    change_host

    newer = launch

    refute_equal running[:dir], newer[:dir]
    assert File.exist?(File.join(running[:dir], "installed-inside"))
    refute File.exist?(File.join(running[:dir], "versions", "3.3", "bin", "gem"))
  end

  def test_its_generation_goes_at_the_first_launch_after_the_sandbox_exits
    running = launch
    change_host
    launch
    exit_sandbox(running)

    launch

    refute File.exist?(running[:dir])
  end

  def test_a_generation_with_read_only_directories_is_still_deleted
    FileUtils.mkdir_p(File.join(@host, "pkg", "mod"))
    File.write(File.join(@host, "pkg", "mod", "file"), "x")
    File.symlink("mod/file", File.join(@host, "pkg", "link"))
    FileUtils.chmod(0o555, File.join(@host, "pkg", "mod"))
    FileUtils.chmod(0o555, File.join(@host, "pkg"))
    first = launch
    exit_sandbox(first)
    change_host

    launch

    refute File.exist?(first[:dir])
  end

  def test_a_build_that_died_half_way_is_cleared
    first = launch
    leftover = "#{first[:dir]}x.tmp-999999"
    FileUtils.mkdir_p(File.join(leftover, "half"))

    launch

    refute File.exist?(leftover)
  end

  # Two launches at once, the host changed: one builds, the other waits and
  # takes what it built.
  def test_two_launches_racing_build_the_new_generation_once
    exit_sandbox(launch)
    change_host
    readers = 2.times.map do
      reader, writer = IO.pipe
      pid = fork do
        reader.close
        SandboxLib.singleton_class.prepend(Module.new do
          def build_generation!(*)
            sleep 0.3 # long enough for the other launch to arrive
            super
          end
        end)
        copy = SandboxLib.prepare_copy!(@host, store: @store)
        writer.write(Marshal.dump(copy.slice(:dir, :built)))
        writer.close
        exit!(0)
      end
      writer.close
      [pid, reader]
    end
    results = readers.map do |pid, reader|
      data = reader.read
      Process.wait(pid)
      Marshal.load(data)
    end

    assert_equal 1, results.count { |r| r[:built] }
    assert_equal 1, results.map { |r| r[:dir] }.uniq.size
  end

  def test_a_copy_wants_a_directory
    File.write(File.join(@home, ".tool-versions"), "ruby 3.3")

    assert_raises(SandboxLib::CopyError) do
      SandboxLib.prepare_copy!(File.join(@home, ".tool-versions"), store: @store)
    end
  end

  def test_a_symlinked_host_path_copies_what_it_points_to
    link = File.join(@home, ".rbenv-link")
    File.symlink(@host, link)

    copy = SandboxLib.prepare_copy!(link, store: @store)
    @locks << copy[:lock]

    refute File.symlink?(copy[:dir])
    assert_equal "host ruby", File.read(File.join(copy[:dir], "versions", "3.3", "bin", "ruby"))
  end

  # A copy is readable by the sandboxes, so it never takes what no sandbox may
  # see, nor the copies themselves.
  def test_what_no_sandbox_may_see_is_never_copied
    uid = Process.uid
    refute_nil SandboxLib.copy_refusal(@home, uid, "#{@home}/.ssh")
    refute_nil SandboxLib.copy_refusal(@home, uid, "#{@home}/.gnupg/private-keys-v1.d")
    refute_nil SandboxLib.copy_refusal(@home, uid, @home)
    refute_nil SandboxLib.copy_refusal(@home, uid, "#{@home}/.sandbox-agent")
    refute_nil SandboxLib.copy_refusal(@home, uid, "#{@home}/.local/share")
    refute_nil SandboxLib.copy_refusal(@home, uid, @store)
    assert_nil SandboxLib.copy_refusal(@home, uid, @host)
    assert_nil SandboxLib.copy_refusal(@home, uid, "#{@home}/.cache/dune")
  end
end

# A launch with copy: lines, on a throwaway home: what is bound where.
class SandboxCopyLaunchTest < Minitest::Test
  def setup
    @real_home = ENV.fetch("HOME")
    @home = Dir.mktmpdir("sandbox-copy-launch-")
    ENV["HOME"] = @home
    File.write(File.join(@home, ".gitconfig"), "")
    @project = File.join(@home, "project")
    FileUtils.mkdir_p(File.join(@home, ".rbenv", "shims"))
    FileUtils.mkdir_p(File.join(@home, ".cache", "dune", "db"))
    FileUtils.mkdir_p(@project)
  end

  def teardown
    ENV["HOME"] = @real_home
    SandboxLib::HELD_COPY_LOCKS.each(&:close).clear
    FileUtils.remove_entry(@home)
  end

  def build(mounts)
    File.write(File.join(@project, ".sandbox-mounts"), mounts)
    args = nil
    _, err = capture_io { args = SandboxLib.build_base_args(@project, git_rw: false) }
    [args, err]
  end

  def binds(args) = args.each_cons(3).select { |flag, _, _| flag == "--bind" }.map { |_, src, dest| [src, dest] }

  # What the launch breaks about the copy (a fake home lacks the dirs some
  # other checks expect, see HOST_DAEMON_DIRS).
  def copy_violations(args, path)
    SandboxLib.invariant_violations(args, home: @home, uid: Process.uid).select { |v| v.include?(path) || v.include?("copies") }
  end

  def copy_bound_at(args, dest)
    binds(args).find { |src, d| d == dest && src.start_with?(File.join(SandboxLib.copy_store(@home), "")) }
  end

  def test_a_copy_is_bound_writable_at_the_host_path
    args, = build("copy:~/.rbenv\n")

    assert copy_bound_at(args, "#{@home}/.rbenv")
    refute_includes binds(args), ["#{@home}/.rbenv", "#{@home}/.rbenv"]
    assert_empty copy_violations(args, ".rbenv")
  end

  # Bound over the sandboxes' own ~/.cache, after it, so it wins.
  def test_a_copy_of_part_of_the_host_s_cache_is_bound_over_the_sandboxes_own
    args, = build("copy:~/.cache/dune\n")

    src, = copy_bound_at(args, "#{@home}/.cache/dune")
    assert src
    own = args.each_cons(3).find_index { |flag, s, d| flag == "--bind" && d == "#{@home}/.cache" && s == SandboxLib.sandbox_cache_dir(@home) }
    copy = args.each_cons(3).find_index { |flag, s, _| flag == "--bind" && s == src }
    assert_operator own, :<, copy
    assert_empty copy_violations(args, "dune")
  end

  def test_rw_of_the_host_s_cache_is_still_dropped_and_suggests_copy
    args, err = build("rw:~/.cache/dune\n")

    refute copy_bound_at(args, "#{@home}/.cache/dune")
    assert_match(/ignored: rw:~\/\.cache\/dune/, err)
    assert_match(/copy:~\/\.cache\/dune/, err)
  end

  def test_codex_s_runtime_is_a_copy_too
    runtime = File.join(@home, ".cache", "codex-runtimes", "codex-primary-runtime")
    FileUtils.mkdir_p(runtime)
    File.write(File.join(runtime, "runtime.json"), "{}")

    args, = build("")

    src, = copy_bound_at(args, runtime)
    assert_equal "{}", File.read(File.join(src, "runtime.json"))
  end

  # Other projects' copies are none of a sandbox's business.
  def test_the_copies_are_hidden_from_the_sandbox
    args, = build("copy:~/.rbenv\n")

    assert_equal "--tmpfs", SandboxLib.effective_mount(args, File.join(SandboxLib.copy_store(@home), "x"))
  end

  def test_a_copy_of_what_no_sandbox_may_see_stops_the_launch
    FileUtils.mkdir_p(File.join(@home, ".ssh"))
    File.write(File.join(@project, ".sandbox-mounts"), "copy:~/.ssh\n")

    assert_raises(SystemExit) { capture_io { SandboxLib.build_base_args(@project, git_rw: false) } }
    assert_empty Dir.glob(File.join(SandboxLib.copy_store(@home), "*ssh*"))
  end
end

# `sandbox-agent allow` shows a copy: line like any other.
class SandboxCopyApprovalTest < Minitest::Test
  def test_approval_shows_copy_lines
    Dir.mktmpdir("copy-approval-") do |root|
      project = File.join(root, "project")
      store = File.join(root, "approvals")
      FileUtils.mkdir_p(project)
      File.write(File.join(project, ".sandbox-mounts"), "copy:~/.rbenv\n")
      out, = capture_io { SandboxLib.approve_mounts!(project, input: StringIO.new("n\n"), store: store) }
      assert_includes out, "+ copy:~/.rbenv"
    end
  end
end
