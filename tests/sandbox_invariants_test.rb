#!/usr/bin/env ruby
# Run: ruby tests/sandbox_invariants_test.rb

require "minitest/autorun"
require "tmpdir"
require_relative "../sandbox-lib"

# build_base_args revokes the host's xhost rule first; a test has no business
# touching the host display.
def SandboxX11.close_host_display!; end

class SandboxInvariantsTest < Minitest::Test
  include SandboxLib

  UID = 1000

  def setup
    @home = Dir.mktmpdir("invariants-home-")
    %w[.ssh .gnupg .password-store project].each { |d| Dir.mkdir(File.join(@home, d)) }
  end

  def teardown
    FileUtils.remove_entry(@home)
  end

  # The smallest argument list that passes: the host environment cleared, and
  # a network of its own.
  def base = ["--clearenv", "--unshare-net"]

  def violations(args) = SandboxLib.invariant_violations(args, home: @home, uid: UID)

  def test_minimal_args_pass
    assert_empty violations(base)
  end

  # The real thing, as built on this machine: today's launch must pass.
  def test_default_launch_passes
    Dir.mktmpdir("invariants-project-") do |project|
      args = SandboxLib.build_base_args(project, git_rw: false)
      assert_empty SandboxLib.invariant_violations(args, home: ENV.fetch("HOME"), uid: Process.uid)
    end
  end

  def test_password_store_is_refused
    refute_empty violations(base + ro("#{@home}/.password-store"))
  end

  def test_gnupg_is_refused
    refute_empty violations(base + ro("#{@home}/.gnupg"))
  end

  def test_host_gpg_agent_socket_dir_is_refused
    refute_empty violations(base + ro("/run/user/#{UID}/gnupg"))
  end

  def test_ssh_dir_is_refused
    refute_empty violations(base + ro("#{@home}/.ssh"))
  end

  def test_session_dbus_socket_is_refused
    refute_empty violations(base + ro("/run/user/#{UID}/bus"))
  end

  def test_docker_socket_is_refused
    refute_empty violations(base + rw("/var/run/docker.sock"))
  end

  # Mounting a parent drags the forbidden path in with it.
  def test_mounting_home_is_refused
    refute_empty violations(base + rw(@home))
  end

  def test_mounting_the_runtime_dir_is_refused
    refute_empty violations(base + ro("/run/user/#{UID}"))
  end

  # bwrap follows symlinks, so a harmless-looking source can still land on one.
  def test_symlink_to_a_forbidden_dir_is_refused
    link = File.join(@home, "project", "keys")
    File.symlink(File.join(@home, ".gnupg"), link)
    refute_empty violations(base + ro(link))
  end

  def test_sibling_with_a_shared_prefix_passes
    Dir.mkdir(File.join(@home, ".sshfs"))
    assert_empty violations(base + ro("#{@home}/.sshfs"))
  end

  def test_unrelated_runtime_socket_passes
    assert_empty violations(base + ro("/run/user/#{UID}/pulse"))
  end

  # A mask binds /dev/null over the path: that hides it, it does not grant it.
  def test_mask_passes
    assert_empty violations(base + mask("#{@home}/.ssh"))
  end

  def test_ssh_agent_variable_is_refused
    refute_empty violations(base + ["--setenv", "SSH_AUTH_SOCK", "/tmp/agent.sock"])
  end

  def test_dbus_variable_is_refused
    refute_empty violations(base + ["--setenv", "DBUS_SESSION_BUS_ADDRESS", "unix:path=/x"])
  end

  def test_missing_clearenv_is_refused
    refute_empty violations(["--unshare-net"])
  end

  def test_shared_network_is_refused
    refute_empty violations(["--clearenv"])
  end

  def test_violation_names_the_path
    assert_match(/\.gnupg/, violations(base + ro("#{@home}/.gnupg")).join("\n"))
  end
end
