#!/usr/bin/env ruby
# Run: ruby tests/sandbox_agents_test.rb

require "minitest/autorun"
require "tmpdir"
require_relative "../sandbox-lib"

# build_base_args revokes the host's xhost rule first; a test has no business
# touching the host display.
def SandboxX11.close_host_display!; end

# Under a fake HOME, `go env` leaves a telemetry process writing to
# ~/.config/go after it exits, racing the cleanup of the temp home.
module SandboxLib
  def self.detect_go_paths = nil
end

# An agent's own state dir must come through rw, or it dies with EROFS.
class SandboxAgentsTest < Minitest::Test
  def with_home(home)
    old = ENV["HOME"]
    ENV["HOME"] = home
    yield
  ensure
    ENV["HOME"] = old
  end

  # The host compiles with this GHC and cabal config: a project asking for them
  # rw is refused, with what to declare instead. Its store may stay writable.
  def test_haskell_toolchain_declared_rw_is_refused
    Dir.mktmpdir("agents-home-") do |home|
      FileUtils.touch(File.join(home, ".gitconfig")) # required by a launch
      %w[.ghcup .config/cabal .local/state/cabal].each { |dir| FileUtils.mkdir_p(File.join(home, dir)) }
      Dir.mktmpdir("agents-project-") do |project|
        File.write(File.join(project, ".sandbox-mounts"),
                   "rw:~/.ghcup\nrw:~/.config/cabal\nrw:~/.local/state/cabal\n")
        with_home(home) do
          args = SandboxLib.build_base_args(project, git_rw: false)
          violations = SandboxLib.invariant_violations(args, home: home, uid: Process.uid)
          assert(violations.any? { |v| v.include?("ro:~/.ghcup") })
          assert(violations.any? { |v| v.include?("ro:~/.config/cabal") })
          refute(violations.any? { |v| v.include?(".local/state/cabal") })
        end
      end
    end
  end

  def test_haskell_toolchain_declared_ro_passes_with_a_writable_store
    Dir.mktmpdir("agents-home-") do |home|
      FileUtils.touch(File.join(home, ".gitconfig")) # required by a launch
      %w[.ghcup .config/cabal .local/state/cabal].each { |dir| FileUtils.mkdir_p(File.join(home, dir)) }
      Dir.mktmpdir("agents-project-") do |project|
        File.write(File.join(project, ".sandbox-mounts"),
                   "ro:~/.ghcup\nro:~/.config/cabal\nrw:~/.local/state/cabal\n")
        with_home(home) do
          args = SandboxLib.build_base_args(project, git_rw: false)
          violations = SandboxLib.invariant_violations(args, home: home, uid: Process.uid)
          refute(violations.any? { |v| v.include?("ghcup") || v.include?("cabal") })
        end
      end
    end
  end

  def test_vibe_home_is_writable
    Dir.mktmpdir("agents-home-") do |home|
      FileUtils.touch(File.join(home, ".gitconfig")) # required by a launch
      vibe_dir = File.join(home, ".vibe")
      Dir.mkdir(vibe_dir)
      Dir.mktmpdir("agents-project-") do |project|
        with_home(home) do
          args = SandboxLib.build_base_args(project, git_rw: false)
          assert_equal "--bind", SandboxLib.effective_bind(args, vibe_dir)
        end
      end
    end
  end
end
