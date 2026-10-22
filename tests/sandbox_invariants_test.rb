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

  # What a real launch adds over ~/.local and friends: see HOST_DAEMON_DIRS.
  def hide_daemons = SandboxLib.host_daemon_dirs(@home).flat_map { |dir| ["--tmpfs", dir] }

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

  # sandbox-agent's own state, all in ~/.sandbox-agent: whoever writes it
  # approves their own mounts and hosts.
  def test_own_state_is_never_writable
    refute_empty violations(base + rw("#{@home}/.sandbox-agent"))
    refute_empty violations(base + rw("#{@home}/.sandbox-agent/approvals"))
    refute_empty violations(base + rw("#{@home}/.sandbox-agent/network-log"))
  end

  # Chrome runs a native messaging host outside any sandbox, picked by the
  # manifests in NativeMessagingHosts: writing either one is running code on
  # the host. See quota_gauges/install.py.
  def test_what_chrome_runs_is_never_writable
    refute_empty violations(base + rw("#{@home}/.local/lib/quota-gauges"))
    refute_empty violations(base + rw("#{@home}/.local/lib/quota-gauges/native-host"))
    refute_empty violations(base + rw("#{@home}/.config/google-chrome/NativeMessagingHosts"))
    refute_empty violations(base + rw("#{@home}/.config/google-chrome"))
    refute_empty violations(base + rw("#{@home}/.config"))
    refute_empty violations(base + rw("#{@home}/.local/lib"))
  end

  # GNOME Shell runs every extension in ~/.local/share/gnome-shell/extensions
  # inside itself, with the whole session's rights.
  def test_what_gnome_shell_runs_is_never_writable
    refute_empty violations(base + rw("#{@home}/.local/share/gnome-shell/extensions"))
    refute_empty violations(base + rw("#{@home}/.local/share/gnome-shell"))
    refute_empty violations(base + rw("#{@home}/.local/share"))
  end

  # The host compiles with this GHC and cabal config (and runs ghcup itself).
  def test_haskell_toolchain_is_never_writable
    refute_empty violations(base + rw("#{@home}/.ghcup"))
    refute_empty violations(base + rw("#{@home}/.ghcup/bin"))
    refute_empty violations(base + rw("#{@home}/.config/cabal"))
  end

  # Host opencode runs the npm packages it finds under its cache, unchecked.
  def test_what_opencode_runs_from_its_cache_is_never_writable
    refute_empty violations(base + rw("#{@home}/.cache") + hide_daemons)
    refute_empty violations(base + ["--tmpfs", "#{@home}/.cache/opencode"] + rw("#{@home}/.cache") + hide_daemons)
  end

  def test_the_rest_of_the_cache_may_be_writable_with_opencode_hidden
    assert_empty violations(base + rw("#{@home}/.cache") + ["--tmpfs", "#{@home}/.cache/opencode"] + hide_daemons)
  end

  def test_haskell_toolchain_may_be_read
    assert_empty violations(base + ro("#{@home}/.ghcup") + ro("#{@home}/.config/cabal") + hide_daemons)
  end

  def test_what_chrome_runs_may_be_read
    assert_empty violations(base + ro("#{@home}/.local/lib/quota-gauges") + hide_daemons)
  end

  def test_home_itself_is_never_writable
    refute_empty violations(base + rw(@home))
  end

  # The one thing a sandbox gets from there: its own network log, read-only.
  def test_own_log_may_be_read
    assert_empty violations(base + ro("#{@home}/.sandbox-agent/network-log/project-0123.log"))
  end

  def test_unrelated_dirs_may_be_writable
    # A tool's own data dir, not the whole of ~/.local/share: that one holds
    # GNOME Shell's extensions (see test_what_gnome_shell_runs_is_never_writable).
    assert_empty violations(base + rw("#{@home}/.local/share/uv") + hide_daemons)
    # A tool's own config dir, not the whole of ~/.config: that one holds
    # NativeMessagingHosts (see test_what_chrome_runs_is_never_writable).
    assert_empty violations(base + rw("#{@home}/.config/htop"))
  end

  # A read-only mount does not stop a connect(): the host watchman's socket,
  # under ~/.local, would take triggers that run commands on the host.
  def test_a_visible_host_daemon_socket_dir_is_refused
    refute_empty violations(base + ro("#{@home}/.local"))
  end

  def test_a_hidden_host_daemon_socket_dir_passes
    assert_empty violations(base + ro("#{@home}/.local") + hide_daemons)
  end

  def test_violation_names_the_path
    assert_match(/\.gnupg/, violations(base + ro("#{@home}/.gnupg")).join("\n"))
  end
end
