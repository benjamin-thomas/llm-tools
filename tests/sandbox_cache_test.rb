#!/usr/bin/env ruby
# Run: ruby tests/sandbox_cache_test.rb

require "minitest/autorun"
require "tmpdir"
require "fileutils"
require_relative "../sandbox-lib"

# The sandboxes' own ~/.cache: what is copied into it from the host's, and when.
class SandboxCacheTest < Minitest::Test
  def setup
    @home = Dir.mktmpdir("sandbox-cache-home-")
    @host_runtime = File.join(@home, ".cache", "codex-runtimes", "codex-primary-runtime")
    FileUtils.mkdir_p(File.join(@host_runtime, "dependencies", "bin"))
    File.write(File.join(@host_runtime, "runtime.json"), '{"version":"1"}')
    File.write(File.join(@host_runtime, "dependencies", "bin", "node"), "host node")
    @copy = File.join(SandboxLib.sandbox_cache_dir(@home), "codex-runtimes", "codex-primary-runtime")
  end

  def teardown
    FileUtils.remove_entry(@home)
  end

  def seed = SandboxLib.seed_sandbox_cache!(@home)

  def test_the_cache_lives_outside_the_host_s_cache
    refute SandboxLib.sandbox_cache_dir(@home).start_with?(File.join(@home, ".cache", ""))
  end

  def test_codex_s_runtimes_are_copied_in_when_missing
    seed

    assert_equal "host node", File.read(File.join(@copy, "dependencies", "bin", "node"))
  end

  def test_the_copy_is_a_copy_never_a_link_to_the_host_s_files
    seed

    host = File.stat(File.join(@host_runtime, "dependencies", "bin", "node"))
    copy = File.stat(File.join(@copy, "dependencies", "bin", "node"))
    refute_equal host.ino, copy.ino
  end

  def test_a_copy_the_sandbox_changed_is_left_while_the_host_s_runtime_is_the_same
    seed
    File.write(File.join(@copy, "dependencies", "bin", "extra"), "fetched inside")

    seed

    assert File.exist?(File.join(@copy, "dependencies", "bin", "extra"))
  end

  def test_a_new_runtime_on_the_host_replaces_the_copy
    seed
    File.write(File.join(@copy, "dependencies", "bin", "node"), "planted")
    File.write(File.join(@host_runtime, "runtime.json"), '{"version":"2"}')

    seed

    assert_equal "host node", File.read(File.join(@copy, "dependencies", "bin", "node"))
  end

  def test_nothing_is_copied_when_the_host_has_no_runtime
    FileUtils.rm_rf(File.join(@home, ".cache"))

    seed

    refute File.exist?(@copy)
  end
end
