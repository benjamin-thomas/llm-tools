#!/usr/bin/env ruby
# Run: ruby tests/sandbox_cache_test.rb

require "minitest/autorun"
require "tmpdir"
require "fileutils"
require_relative "../sandbox-lib"

# The sandboxes' own ~/.cache. What is copied into it from the host's is
# copy:'s business: see tests/sandbox_copy_test.rb.
class SandboxCacheTest < Minitest::Test
  def setup
    @home = Dir.mktmpdir("sandbox-cache-home-")
  end

  def teardown
    FileUtils.remove_entry(@home)
  end

  def test_the_cache_lives_outside_the_host_s_cache
    refute SandboxLib.sandbox_cache_dir(@home).start_with?(File.join(@home, ".cache", ""))
  end

  def test_codex_s_runtime_is_a_built_in_copy
    assert_includes SandboxLib.default_copies(@home), File.join(@home, ".cache", "codex-runtimes", "codex-primary-runtime")
  end
end
