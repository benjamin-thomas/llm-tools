#!/usr/bin/env ruby

# Shared sandbox construction for `sandbox-agent` and `run-orca-server`.
#
# The two tools do different jobs — one opens an interactive agent session, the
# other starts an Orca server — but they grant the same thing, because agents
# run inside either way. Hence one definition of what gets mounted, one floor,
# one .sandbox-mounts contract. Two copies would drift, and a drifted mount rule
# is exactly how the auto-mounted-symlink hole got in.
#
# What stays in the callers: X11/audio and the multiplexer (interactive only),
# Orca's userData, port and pairing (server only).

require "digest"
require "fileutils"
require "open3"
require "shellwords"

module SandboxLib
  module_function

  def detect_opam_switch
    config = File.expand_path("~/.opam/config")
    return nil unless File.exist?(config)
    match = File.read(config).match(/^switch:\s*"([^"]+)"/)
    match && match[1]
  end

  # Locate a Chrome/Chromium install on the host so projects driving a browser
  # via CDP (e.g. puppeteer-core, playwright in connect mode) don't each need
  # to declare `ro:/opt/google/chrome` in `.sandbox-mounts`. Returns the install
  # dir to mount, or nil if no system Chrome/Chromium is found.
  def detect_chrome_install_dir
    candidates = [
      "/opt/google/chrome/google-chrome",
      "/opt/google/chrome-stable/google-chrome",
      "/usr/bin/google-chrome",
      "/usr/bin/google-chrome-stable",
      "/usr/bin/chromium",
      "/usr/bin/chromium-browser",
      "/snap/bin/chromium",
    ]
    candidates.each do |bin|
      next unless File.exist?(bin) || File.symlink?(bin)
      target = File.realpath(bin) rescue bin
      dir = File.dirname(target)
      return dir if File.directory?(dir)
    end
    nil
  end

  # Resolve the Go toolchain the caller's shell actually uses. Rather than hardcode
  # a GOROOT location (it varies a lot: /usr/local/go, distro /usr/bin, asdf, or a
  # JetBrains/golang.org-dl dir like ~/sdk/<version>), find the first `go` on PATH
  # and ask it for GOROOT/GOPATH, so the sandbox tracks the same version as the
  # shell launching it. Returns {root:, path:} or nil. (GOPATH may be a list — we
  # take the first entry, the one `go install` writes to.)
  def detect_go_paths
    go = ENV["PATH"].to_s.split(File::PATH_SEPARATOR)
                   .map { |dir| File.join(dir, "go") }
                   .find { |p| File.file?(p) && File.executable?(p) }
    return nil unless go

    out, _err, status = Open3.capture3(go, "env", "GOROOT", "GOPATH")
    return nil unless status.success?

    root, path = out.lines(chomp: true)
    { root: root, path: path.to_s.split(File::PATH_SEPARATOR).first }
  end


  def ro(src, dest = src) = ["--ro-bind", src, dest]
  def rw(src, dest = src) = ["--bind",    src, dest]

  # Shadow a path that an earlier mount dragged in: bind /dev/null over it, so it
  # is still there but useless — exec fails, connect() fails (it is no longer a
  # socket). Later mounts win in bwrap, so masks must come after their mount.
  def mask(path) = ["--ro-bind", "/dev/null", path]

  def require_dir!(path, label)
    abort "#{label} not found: #{path}" unless File.directory?(path)
  end

  def require_file!(path, label)
    abort "#{label} not found: #{path}" unless File.exist?(path)
  end

  def find_command_path(command, path_dirs)
    return nil if command.nil? || command.empty?

    if command.include?("/")
      expanded = File.expand_path(command)
      return expanded if File.exist?(expanded) || File.symlink?(expanded)
      return nil
    end

    path_dirs.each do |dir|
      candidate = File.join(dir, command)
      return candidate if File.exist?(candidate) || File.symlink?(candidate)
    end

    nil
  end

  # Later mounts win in bwrap, so a ro bind added for a command's symlink target
  # would shadow a rw mount made earlier — e.g. ~/.grok holds both the `grok`
  # binary and the state it writes, likewise ~/.local/share/claude. Detect that.
  def bound_rw?(args, path)
    args.each_index.any? do |i|
      next false unless args[i] == "--bind"
      src = args[i + 1]
      path == src || path.start_with?("#{src}/")
    end
  end

  def bind_command_symlink_target!(args, command, path_dirs)
    command_path = find_command_path(command, path_dirs)
    return unless command_path && File.symlink?(command_path)

    target = File.realpath(command_path)
    mount_path = File.directory?(target) ? target : File.dirname(target)
    return unless File.directory?(mount_path)
    return if bound_rw?(args, mount_path)

    warn "Binding command symlink target: #{mount_path}"
    args.push(*ro(mount_path))
  end

  def git_metadata_paths(repo_dir)
    out, _err, status = Open3.capture3(
      "git",
      "-C",
      repo_dir,
      "rev-parse",
      "--path-format=absolute",
      "--git-dir",
      "--git-common-dir"
    )
    return [] unless status.success?

    paths = out.lines(chomp: true).reject(&:empty?)
    git_file = File.join(repo_dir, ".git")
    paths << git_file if File.exist?(git_file)
    paths.uniq.select { |path| File.exist?(path) }
  end

  def bind_git_metadata!(args, repo_dir, writable:)
    git_metadata_paths(repo_dir).each do |path|
      args.push(*(writable ? rw(path) : ro(path)))
    end
  end

  # The one way to grant write access to a directory: code writable, git history
  # read-only unless --git-rw. Guarded on the mount root being a repo root, so a
  # repo nested inside a mount (ruby-advisory-db under ~/.local/share, which
  # bundle-audit updates via git fetch) keeps the writable .git its tooling needs.
  def bind_rw_mount!(args, path, git_writable:)
    args.push(*rw(path))
    return unless File.exist?(File.join(path, ".git"))

    bind_git_metadata!(args, path, writable: git_writable)
  end

  # Parse .sandbox-mounts into [{mode:, path:, target:, line:}].
  #
  # `path` is where we mount (as written, ~ expanded) and `target` is that with
  # symlinks resolved. The floor is checked against `target`, never `path`:
  # File.expand_path does not resolve symlinks, so a project-local symlink would
  # otherwise resolve to something other than what you read. We still mount at
  # `path` so a declared dir that happens to be a symlink keeps its expected
  # location inside the sandbox.
  def parse_sandbox_mounts(project_dir, home)
    file = File.join(project_dir, ".sandbox-mounts")
    return [] unless File.exist?(file)

    File.readlines(file).filter_map do |raw|
      line = raw.strip
      next if line.empty? || line.start_with?("#")

      line = line.sub(/\s+#.*\z/, "") # strip inline comments (recipes are copy-paste ready)
      mode, path = line.split(":", 2)
      if path.nil? || path.strip.empty?
        warn "Malformed line in .sandbox-mounts: #{line}"
        next
      end

      expanded = File.expand_path(path.strip)
      target   = begin File.realpath(expanded) rescue expanded end
      { mode: mode, path: expanded, target: target, line: line }
    end
  end

  # Top-level symlinks that leave the project, and whether .sandbox-mounts already
  # declares each one. Returns [{name:, target:, declared:}].
  def project_escaping_symlinks(project_dir, declared_mounts)
    declared = declared_mounts.flat_map { |m| [m[:path], m[:target]] }.uniq

    Dir.children(project_dir).sort.filter_map do |name|
      full = File.join(project_dir, name)
      next unless File.symlink?(full) && File.exist?(full)

      target = begin File.realpath(full) rescue next end
      next if target.start_with?("#{project_dir}/") # resolves inside the project
      next unless File.directory?(target)

      covered = declared.any? { |d| target == d || target.start_with?("#{d}/") }
      { name: name, target: target, declared: covered }
    end
  end


  TOOLCHAIN_HINTS = [
    { glob: "*.cabal",          name: "Haskell (ghcup + cabal)",
      lines: ["rw:~/.ghcup", "path:~/.ghcup/bin", "rw:~/.config/cabal", "rw:~/.local/state/cabal"] },
    { glob: "cabal.project",    name: "Haskell (ghcup + cabal)",
      lines: ["rw:~/.ghcup", "path:~/.ghcup/bin", "rw:~/.config/cabal", "rw:~/.local/state/cabal"] },
    { glob: "package.json",     name: "Node — pick your version manager",
      lines: ["rw:~/.nvm", "path:~/.nvm/versions/node/<version>/bin", "# or volta / fnm — see --help"] },
    { glob: "Gemfile",          name: "Ruby (rbenv)",
      lines: ["rw:~/.rbenv", "rw:~/.bundle", "path:~/.rbenv/shims", "path:~/.rbenv/bin"] },
    { glob: "Cargo.toml",       name: "Rust (rustup + cargo)",
      lines: ["ro:~/.rustup", "rw:~/.cargo", "path:~/.cargo/bin"] },
    { glob: "dune-project",     name: "OCaml (opam)",
      lines: ["ro:~/.opam", "path:~/.opam/<switch>/bin"] },
    { glob: "pyproject.toml",   name: "Python — pick your tool",
      lines: ["rw:~/.cache/uv", "ro:~/.local/share/uv", "path:~/.local/bin", "# or pyenv / pipx — see --help"] },
    { glob: "go.mod",           name: "Go",
      lines: ["# nothing needed — the toolchain on your PATH is auto-detected"] },
  ].freeze

  # `sandbox-agent doctor <project>` — report what this project must declare before
  # it will run under the allowlist, and optionally write the unambiguous part.
  def run_doctor(project_dir, home, write:)
    mounts_file = File.join(project_dir, ".sandbox-mounts")
    declared = parse_sandbox_mounts(project_dir, home)
    puts "Project:        #{project_dir}"
    puts ".sandbox-mounts: #{File.exist?(mounts_file) ? "#{declared.size} declaration(s)" : "absent"}"
    puts

    escaping = project_escaping_symlinks(project_dir, declared)
    missing  = escaping.reject { |s| s[:declared] }

    unless escaping.empty?
      puts "Symlinks leaving the project:"
      escaping.each do |s|
        puts "  #{s[:declared] ? "ok      " : "MISSING "} #{s[:name]} -> #{s[:target]}"
      end
      puts
    end

    if missing.empty?
      puts "No undeclared symlink targets." if escaping.empty?
    else
      puts "The symlink grants nothing by itself. Each target resolves only if some"
      puts "other mount already covers it, and then only at that mount's access level"
      puts "— e.g. anything under ~/.local is readable but not writable. Declare it to"
      puts "get a specific, reviewable grant:"
      puts
      missing.each { |s| puts "  rw:#{s[:target]}" }
      puts
      puts "(rw: assumed above; use ro: where the project only reads through the link)"
      puts
    end

    # One level down too: these repos keep backend/ and frontend/ side by side, so
    # a root-only glob would miss every toolchain they actually use.
    hints = TOOLCHAIN_HINTS.select do |h|
      !Dir.glob(File.join(project_dir, h[:glob])).empty? ||
        !Dir.glob(File.join(project_dir, "*", h[:glob])).empty?
    end.uniq { |h| h[:name] }
    unless hints.empty?
      puts "Toolchains detected (advisory — check against what you actually use):"
      hints.each do |hint|
        puts "  # #{hint[:name]}"
        hint[:lines].each { |line| puts "  #{line}" }
      end
      puts
    end

    if write && !missing.empty?
      header = File.exist?(mounts_file) ? "" : <<~HEAD
        # Extra mounts for sandbox-agent (one per line)
        # Formats: ro:/path, rw:/path, path:/path/bin
      HEAD
      File.open(mounts_file, "a") do |io|
        io.puts(header) unless header.empty?
        io.puts
        io.puts "# Added by `sandbox-agent doctor --write`: symlink targets that leave the"
        io.puts "# project. Review each one — rw: was assumed; ro: is the safer default."
        missing.each { |s| io.puts "rw:#{s[:target]}" }
      end
      puts "Wrote #{missing.size} declaration(s) to #{mounts_file}."
    elsif !missing.empty?
      puts "Re-run with --write to append them."
    end

    missing.empty? ? 0 : 1
  end


  # --- Mount approval ------------------------------------------------------
  #
  # WHY THERE IS NO DENYLIST HERE, AND WHY YOU SHOULD NOT ADD ONE BACK.
  #
  # This file used to carry a "floor": a list of paths .sandbox-mounts could
  # never grant (~/.ssh, the keyring, D-Bus, the Orca socket, docker.sock). That
  # was a patch over a deeper flaw.
  #
  # .sandbox-mounts lives INSIDE the project, which the agent can write. An
  # allowlist is only a security control when the trusted party writes it —
  # otherwise it is self-service, and the agent grants itself whatever it likes
  # on the next launch. A denylist does not fix that; it only moves the question
  # to "did I remember to forbid everything?", which is endless and which you
  # always eventually lose. That is exactly how the auto-mounted-symlink hole got
  # in: one rule enforced in one code path and bypassed in another.
  #
  # So: .sandbox-mounts is a REQUEST, not an authorization. Approval is recorded
  # outside the project, out of the agent's reach. Any edit to the file
  # invalidates it and forces a human to read it again. This is the `direnv
  # allow` model.
  #
  # The digest covers the CONTENT, not the path: a new worktree with the same
  # .sandbox-mounts is already approved — no friction in a worktree-heavy flow —
  # while an edit, in any worktree, asks for a fresh read.
  #
  # If you find yourself wanting to add "just a small denylist to be safe": it
  # would be redundant. A dangerous line is visible at approval time, which is
  # the one place a human actually looks.
  APPROVAL_DIR = File.expand_path("~/.local/share/sandbox-agent/approved")

  def mounts_file(project_dir) = File.join(project_dir, ".sandbox-mounts")

  # nil when the project declares nothing: there is then nothing to approve.
  def mounts_digest(project_dir)
    file = mounts_file(project_dir)
    File.exist?(file) ? Digest::SHA256.hexdigest(File.read(file)) : nil
  end

  def mounts_approved?(project_dir)
    digest = mounts_digest(project_dir)
    digest.nil? || File.exist?(File.join(APPROVAL_DIR, digest))
  end

  # Where the last approved content for THIS project is remembered. The store is
  # keyed by content hash, which cannot answer "what did it look like before?" —
  # so a per-project pointer records which hash was last approved, and that is
  # what the diff below compares against.
  def approval_pointer(project_dir)
    File.join(APPROVAL_DIR, "by-project", Digest::SHA256.hexdigest(project_dir)[0, 16])
  end

  def approved_content(project_dir)
    pointer = approval_pointer(project_dir)
    return nil unless File.exist?(pointer)

    previous = File.join(APPROVAL_DIR, File.read(pointer).strip)
    File.exist?(previous) ? File.read(previous) : nil
  end

  def approve_mounts!(project_dir)
    digest = mounts_digest(project_dir)
    if digest.nil?
      warn "#{project_dir} declares no mounts — nothing to approve."
      return 0
    end

    puts "Mounts requested by #{mounts_file(project_dir)}:"
    puts
    parse_sandbox_mounts(project_dir, ENV.fetch("HOME")).each { |m| puts "  #{m[:mode]}:#{m[:path]}" }
    puts
    FileUtils.mkdir_p(File.join(APPROVAL_DIR, "by-project"))
    FileUtils.chmod(0o700, APPROVAL_DIR) rescue nil
    # The content, not an empty marker: it is what a later diff compares against.
    File.write(File.join(APPROVAL_DIR, digest), File.read(mounts_file(project_dir)))
    File.write(approval_pointer(project_dir), digest)
    puts "Approved. Any edit to the file will ask again."
    0
  end

  # A plain line diff: what the approved version had and the current one does not,
  # then the reverse. Not a real LCS diff — for a short declaration file, "gone"
  # and "added" is what you need to judge, and it needs no external tool.
  def diff_mount_lines(previous, current)
    old_lines = previous.lines.map(&:chomp)
    new_lines = current.lines.map(&:chomp)
    removed = old_lines - new_lines
    added = new_lines - old_lines
    lines = removed.reject(&:empty?).map { |l| "- #{l}" }
    lines += added.reject(&:empty?).map { |l| "+ #{l}" }
    lines.empty? ? ["(only blank lines or reordering changed)"] : lines
  end

  def require_mounts_approval!(project_dir, tool)
    return if mounts_approved?(project_dir)

    previous = approved_content(project_dir)
    current = File.read(mounts_file(project_dir))

    if previous
      warn "#{mounts_file(project_dir)} changed since you approved it."
      warn ""
      warn "What changed (- was approved, + is new):"
      warn ""
      diff_mount_lines(previous, current).each { |line| warn "  #{line}" }
    else
      warn "#{mounts_file(project_dir)} has never been approved."
      warn ""
      warn "It requests:"
      warn ""
      parse_sandbox_mounts(project_dir, ENV.fetch("HOME")).each { |m| warn "  #{m[:mode]}:#{m[:path]}" }
    end
    warn ""
    warn "This file lives in the project, so an agent may have edited it to grant"
    warn "itself new access. Read the above, then run:"
    warn ""
    warn "  sandbox-agent allow #{project_dir}"
    warn ""
    warn "(#{tool} refuses to start until you do.)"
    exit 1
  end

  # Every bwrap argument the two tools share, in the order bwrap needs — last
  # mount wins, so the ordering here is load-bearing, not stylistic.
  #
  # headless      no host X11: drops the display grant and lets a sandbox-local
  #               Xvfb create its own socket, which the read-only host mount of
  #               /tmp/.X11-unix would otherwise prevent.
  # extra_mounts  operator-supplied "MODE:SRC[:DEST]" strings. Applied after the
  #               project's own .sandbox-mounts so they win a conflict, and
  #               before the floor masks so they cannot defeat them.
  # command  the executable that will run inside, when the caller knows it. Only
  #          used to resolve a symlinked launcher's target (a CLI whose binary
  #          lives beside the state it writes), so nil is fine.
  def build_base_args(project_dir, git_rw:, headless:, command: nil, extra_mounts: [], extra_env: [])
    # --- detect paths ---

    home = ENV.fetch("HOME")
    term = ENV.fetch("TERM", "xterm-256color")

    nvm_bin        = ENV["NVM_BIN"] || `bash -c 'source #{home}/.nvm/nvm.sh 2>/dev/null && echo -n "$NVM_BIN"'`
    nvm_bin        = nil if nvm_bin.empty?
    gitconfig      = "#{home}/.gitconfig"
    gitignore      = "#{home}/.gitignore"
    password_store = "#{home}/.password-store"
    gnupg_dir      = "#{home}/.gnupg"
    gpg_agent_dir  = "/run/user/#{Process.uid}/gnupg"
    resolvconf_dir =
      if    Dir.exist?("/run/resolvconf")       then "/run/resolvconf"
      elsif Dir.exist?("/run/systemd/resolve")  then "/run/systemd/resolve"
      else  abort "Cannot find DNS resolver directory (/run/resolvconf or /run/systemd/resolve)"
      end

    # --- validate required paths ---

    require_dir!  project_dir,    "Project directory"
    require_file! gitconfig,      "Git config"
    require_dir!  password_store, "Password store (~/.password-store)"
    require_dir!  gnupg_dir,      "GnuPG directory (~/.gnupg)"
    require_dir!  gpg_agent_dir,  "GPG agent socket dir"
    require_dir!  resolvconf_dir, "DNS resolver"

    # --- build bwrap args ---

    args = []

    # System directories (read-only)
    args.push(*ro("/usr"), *ro("/lib"), *ro("/lib64"),
              *ro("/bin"), *ro("/sbin"), *ro("/etc"))
    args.push("--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp")

    # Chrome (and a lot of other tooling) wants /dev/shm. bwrap's --dev mounts a
    # minimal devtmpfs without it, so add an explicit tmpfs.
    args.push("--tmpfs", "/dev/shm")

    # Auto-mount Chrome/Chromium install dir so projects can drive a CDP browser
    # without per-project .sandbox-mounts entries.
    chrome_dir = detect_chrome_install_dir
    args.push(*ro(chrome_dir)) if chrome_dir

    # X11 clipboard access.
    #
    # Skipped under --headless, and not only to drop the grant: the host mount is
    # read-only, so a sandbox-local Xvfb cannot create its own socket underneath it
    # and dies with "Xvfb did not become ready". Leaving the dir out entirely lets
    # the tmpfs /tmp supply a writable one.
    x11_socket = "/tmp/.X11-unix"
    xauthority = nil
    unless headless
      args.push(*ro(x11_socket)) if File.directory?(x11_socket)
      xauthority = ENV["XAUTHORITY"]&.then { |path| File.expand_path(path) }
      xauthority = nil unless xauthority && File.file?(xauthority)
      args.push(*ro(xauthority)) if xauthority
    end

    # Chromium reads /sys/devices/system/cpu to size its worker pools. Harmless to
    # expose (read-only kernel metadata, no secrets) and Electron logs errors on
    # every start without it.
    args.push(*ro("/sys")) if headless && File.directory?("/sys")

    # IDE connection sockets
    claude_ipc = "/tmp/claude-#{Process.uid}"
    args.push(*rw(claude_ipc)) if File.directory?(claude_ipc)

    # DNS
    args.push(*ro(resolvconf_dir))

    # Audio devices for OMP live/STT and direct-ALSA speech/dictation.
    # Must be --dev-bind: a plain --bind/--ro-bind mounts the nodes nodev, so every
    # open() of /dev/snd/control* fails and ALSA reports "no soundcards found"
    # (`arecord -l` empty, hw:/plughw: devices unusable). Only matters for the
    # direct-ALSA path — capture through the sound server below needs no device.
    snd_dir = "/dev/snd"
    args.push("--dev-bind", snd_dir, snd_dir) if File.directory?(snd_dir)

    # PulseAudio / PipeWire sockets. OMP's native Linux voice backend prefers
    # PulseAudio and falls back to ALSA; recorder CLIs use these sockets too.
    pulse_dir       = "/run/user/#{Process.uid}/pulse"
    pipewire_socket = "/run/user/#{Process.uid}/pipewire-0"
    args.push(*ro(pulse_dir))       if File.directory?(pulse_dir)
    args.push(*ro(pipewire_socket)) if File.exist?(pipewire_socket)

    # No D-Bus session socket and no keyring dir: both are the Secret Service (and
    # the keyring dir also holds an ssh agent). See the header — do not re-add.

    # Home: tmpfs overlay, then selective mounts
    args.push("--tmpfs", home)
    args.push(*ro(gitconfig))
    args.push(*ro(gitignore)) if File.exist?(gitignore)

    # Agent config/data directories
    local_dir = "#{home}/.local"
    claude_dir = "#{home}/.claude"
    claude_json = "#{home}/.claude.json"
    codex_dir   = "#{home}/.codex"
    args.push(*ro(local_dir))    if File.directory?(local_dir)
    args.push(*rw(claude_dir))   if File.directory?(claude_dir)
    args.push(*rw(claude_json))  if File.exist?(claude_json)
    # ~/.claude comes through rw, symlinks and all, so a statusline.rb linked out
    # to a repo dangles in every other project's sandbox. Mount the target ro --
    # the host runs this file on a timer, outside the sandbox. Skipped when it is
    # inside the project, already covered rw.
    statusline = "#{claude_dir}/statusline.rb"
    if File.symlink?(statusline)
      target = begin
        File.realpath(statusline)
      rescue SystemCallError
        nil
      end
      if target && !target.start_with?("#{project_dir}/")
        args.push(*ro(target))
      end
    end
    # Native Claude Code install: runtime state/lock + versioned binaries live under
    # ~/.local, which is mounted read-only above. Overlay these subtrees rw so the
    # native `claude` binary can take its launch lock and self-manage versions.
    claude_state_dir = "#{home}/.local/state/claude"
    claude_share_dir = "#{home}/.local/share/claude"
    args.push(*rw(claude_state_dir)) if File.directory?(claude_state_dir)
    args.push(*rw(claude_share_dir)) if File.directory?(claude_share_dir)
    args.push(*rw(codex_dir))    if File.directory?(codex_dir)
    # Kimi Code: ~/.kimi-code holds the binary plus OAuth locks, sessions and logs
    # (all written at runtime — rw), ~/.kimi holds config/credentials (rw),
    # ~/.config/kimi holds user agents (rw).
    kimi_dir        = "#{home}/.kimi"
    kimi_config_dir = "#{home}/.config/kimi"
    kimi_code_dir   = "#{home}/.kimi-code"
    args.push(*rw(kimi_dir))        if File.directory?(kimi_dir)
    args.push(*rw(kimi_config_dir)) if File.directory?(kimi_config_dir)
    args.push(*rw(kimi_code_dir))   if File.directory?(kimi_code_dir)
    # Kimi user skills live in ~/.agents/skills as symlinks into this repo's
    # global-skills. Mount rw, not ro: llm-skills install/import manage those links,
    # and skills are curated from inside a sandbox session. ~/.claude and ~/.codex
    # are already rw above, so a ro mount here made kimi the one CLI whose skills
    # could not be installed without leaving the sandbox (EROFS on symlink create).
    # Pairs with the rw global-skills overlay below — editing a skill needs the
    # content writable, (re)installing one needs the link directory writable.
    agents_dir = "#{home}/.agents"
    args.push(*rw(agents_dir)) if File.directory?(agents_dir)
    opencode_cfg = "#{home}/.config/opencode"
    opencode_data = "#{home}/.local/share/opencode"
    # ~/.local/state/opencode holds runtime locks/state and falls under the ro
    # ~/.local mount above — overlay it rw or opencode crashes with EROFS trying to
    # create its lock files (mkdir .../locks/<hash>.lock).
    opencode_state = "#{home}/.local/state/opencode"
    args.push(*rw(opencode_cfg))   if File.directory?(opencode_cfg)
    args.push(*rw(opencode_data))  if File.directory?(opencode_data)
    args.push(*rw(opencode_state)) if File.directory?(opencode_state)
    # pi: ~/.pi holds config/auth plus runtime state (sessions, logs), all written
    # at runtime — mount rw. pi is yolo by default.
    pi_dir = "#{home}/.pi"
    args.push(*rw(pi_dir)) if File.directory?(pi_dir)
    # Oh My Pi: ~/.omp holds config/auth plus runtime-written sessions, logs,
    # caches, extracted native addons, and daemon/browser state.
    omp_dir = "#{home}/.omp"
    args.push(*rw(omp_dir)) if File.directory?(omp_dir)

    # Prime Agent — a pi fork, so the same shape one directory over. ~/.prime/agent
    # is the config dir (auth.json, models.json, settings.json, keybindings.json)
    # and also holds everything written at runtime: sessions, session-leases,
    # session-artifacts, daemon-workers, logs, plus kernel-venv, the Python venv it
    # bootstraps for its IPython tool. ~/.prime/supervisor-owners sits one level up
    # (the daemon ownership registry), so bind the whole tree rw rather than just
    # the agent dir. PRIME_AGENT_CODING_AGENT_DIR relocates the agent dir; honour it
    # like GROK_HOME/ASDF_DATA_DIR are honoured, and pass it through below.
    prime_dir = "#{home}/.prime"
    args.push(*rw(prime_dir)) if File.directory?(prime_dir)
    prime_agent_dir = ENV["PRIME_AGENT_CODING_AGENT_DIR"]&.then { |d| File.expand_path(d) }
    args.push(*rw(prime_agent_dir)) if prime_agent_dir && File.directory?(prime_agent_dir)
    # Note what is deliberately NOT reachable here: prime-agent's daemon socket is
    # $TMPDIR/prime-agent-<uid>/daemon.sock, and that socket starts agent processes
    # for whoever holds it — the same escape the mux sockets below guard against.
    # The sandbox gets its own because /tmp is a tmpfs and TMPDIR is not passed
    # through, so the host daemon is simply not there. Mounting /tmp (or exporting
    # TMPDIR) would hand a sandboxed agent the host's daemon; don't.
    #
    # A private socket is only half of it. prime-agent runs its agents in a daemon
    # and tracks that daemon in three directories under ~/.prime, by raw PID and by
    # socket path — both of which are namespace-local here, since the sandbox has
    # its own PID namespace (--unshare-pid, below) and its own /tmp. Leave them
    # shared and a sandboxed daemon overwrites the *host's* bookkeeping with numbers
    # that mean nothing outside the sandbox:
    #
    #   supervisor-owners/    the daemon ownership registry. A sandbox claiming it
    #                         orphans the host's live supervisor, which then fails
    #                         every launch with "no longer owns its registry entry".
    #   agent/daemon-workers/ per-worker descriptors, likewise pid-keyed.
    #   agent/session-leases/ who currently holds a session. Worst of the three: a
    #                         namespaced pid can *alias a live host process* (a
    #                         leftover lease for "pid 124" matched a kernel thread),
    #                         so the staleness check never fires and the session
    #                         stays locked forever.
    #
    # One private tmpfs each fixes both directions at once — the host is protected,
    # and any number of sandboxes get fully independent daemons. Everything worth
    # sharing still is: auth.json, models.json, settings.json, keybindings.json,
    # sessions/, session-artifacts/ and the (expensive) kernel venv.
    %w[supervisor-owners agent/daemon-workers agent/session-leases].each do |sub|
      args.push("--tmpfs", "#{prime_dir}/#{sub}")
    end

    # grok (xAI): everything lives under one dir (GROK_HOME, default ~/.grok) — the
    # binary itself (~/.local/bin/grok symlinks into downloads/), config.toml,
    # auth.json, plus lock files, sessions, logs and worktrees.db written at
    # runtime. Mount rw. Its bin/ goes on PATH below so `grok` resolves even when
    # the ~/.local/bin symlink is absent.
    grok_dir = ENV["GROK_HOME"]&.then { |d| File.expand_path(d) } || "#{home}/.grok"
    args.push(*rw(grok_dir)) if File.directory?(grok_dir)

    # VS Code / Devin Desktop (Electron IDEs). The binaries resolve under
    # /usr/share (already mounted ro), so only the per-user state under $HOME
    # needs binding — and rw, since Electron writes caches, lock files and
    # state DBs continuously. VS Code keeps user data in ~/.config/Code and
    # extensions in ~/.vscode; Devin (a VS Code fork) mirrors this with
    # ~/.config/Devin + ~/.devin, plus a small ~/.config/devin (cli/config).
    vscode_config = "#{home}/.config/Code"
    vscode_exts   = "#{home}/.vscode"
    devin_config  = "#{home}/.config/Devin"
    devin_cli     = "#{home}/.config/devin"
    devin_exts    = "#{home}/.devin"
    args.push(*rw(vscode_config)) if File.directory?(vscode_config)
    args.push(*rw(vscode_exts))   if File.directory?(vscode_exts)
    args.push(*rw(devin_config))  if File.directory?(devin_config)
    args.push(*rw(devin_cli))     if File.directory?(devin_cli)
    args.push(*rw(devin_exts))    if File.directory?(devin_exts)
    # Devin's agent backend ("devin acp", a Rust binary from the windsurf/codeium
    # stack) keeps state outside ~/.config/Devin: logs + sessions.db live under
    # ~/.local/share/devin, which falls under the ro ~/.local mount above — overlay
    # it rw or the binary panics with ReadOnlyFilesystem creating its rolling log.
    # ~/.codeium holds the matching codeium/windsurf auth + state.
    devin_share = "#{home}/.local/share/devin"
    codeium_dir = "#{home}/.codeium"
    args.push(*rw(devin_share)) if File.directory?(devin_share)
    args.push(*rw(codeium_dir)) if File.directory?(codeium_dir)

    # MoonBit toolchain (~/.moon): the IDE spawns ~/.moon/bin/moon-lsp by absolute
    # path, and `moon` writes build/registry caches here, so mount rw (like cargo).
    # Its bin/ is added to PATH below so the moon CLI and the tools it shells out
    # to (moonc, moonfmt, ...) resolve in the sandbox terminal too.
    moon_dir = "#{home}/.moon"
    args.push(*rw(moon_dir)) if File.directory?(moon_dir)

    # asdf version manager. The `asdf` binary lives in ~/.local/bin (already on
    # PATH), but its data dir (ASDF_DATA_DIR, default ~/.asdf) holds the plugins,
    # installs, shims and downloads — hidden by the tmpfs home. Mount it rw (asdf
    # writes downloads/, installs/, tmp/), add its shims/ to PATH below, and pass
    # ASDF_DATA_DIR through so a non-default location is honoured.
    asdf_dir = ENV["ASDF_DATA_DIR"]&.then { |d| File.expand_path(d) } || "#{home}/.asdf"
    args.push(*rw(asdf_dir)) if File.directory?(asdf_dir)

    # Go toolchain + GOPATH, tracking whatever `go` is on the caller's PATH (see
    # detect_go_paths). GOROOT is mounted ro; GOPATH rw (module cache + 'go install'
    # bins). Both bin dirs go on PATH below. GOCACHE (~/.cache/go-build) is already
    # covered by the ~/.cache mount.
    go_paths = detect_go_paths
    if go_paths
      args.push(*ro(go_paths[:root])) if go_paths[:root] && File.directory?(go_paths[:root])
      args.push(*rw(go_paths[:path])) if go_paths[:path] && File.directory?(go_paths[:path])
    end

    # bun: globally-installed CLIs (opencode, etc.) live under ~/.bun as standalone
    # binaries symlinked from ~/.bun/bin. Mount ro and add ~/.bun/bin to PATH below
    # so `bun`/`bunx` and bun-installed tools resolve (mirrors the nvm handling).
    bun_dir = "#{home}/.bun"
    args.push(*ro(bun_dir)) if File.directory?(bun_dir)

    # WakaTime: ~/.wakatime.cfg holds the api key (ro), ~/.wakatime holds the
    # bundled cli + offline-heartbeat db + internal cfg (rw — the cli writes here).
    wakatime_cfg = "#{home}/.wakatime.cfg"
    wakatime_dir = "#{home}/.wakatime"
    args.push(*ro(wakatime_cfg)) if File.exist?(wakatime_cfg)
    args.push(*rw(wakatime_dir)) if File.directory?(wakatime_dir)

    # The llm-tools repo backs a set of ~/.local/bin tools and CLI skills through
    # symlinks that resolve back into it: tmux-orchestrator (+ tmux_orchestrator.py),
    # llm-skills, and the per-CLI skills under ~/.claude/skills, ~/.codex/skills,
    # ~/.agents/skills, etc. Mount the repo root ro so those symlinks resolve inside
    # the sandbox AND the coordinator can invoke tmux-orchestrator at runtime.
    # Skipped when the sandboxed project already covers the repo (it is the repo, or
    # a parent/child of it) to avoid a conflicting double bind. global-skills is a
    # subpath, so the skill symlinks are covered by this single mount.
    llm_tools_root = "#{home}/code/github.com/benjamin-thomas/llm-tools"
    llm_tools_covered =
      project_dir == llm_tools_root ||
      project_dir.start_with?("#{llm_tools_root}/") ||
      llm_tools_root.start_with?("#{project_dir}/")
    args.push(*ro(llm_tools_root)) if File.directory?(llm_tools_root) && !llm_tools_covered

    # global-skills stays writable even though the repo around it is ro: skills are
    # edited from inside a sandbox session, and a ro mount makes that impossible
    # without leaving the sandbox. Later mounts win in bwrap, so this rw bind
    # overlays the ro repo mount above. When the sandboxed project *is* llm-tools
    # the rw project mount below already covers it, hence the same guard.
    global_skills_dir = "#{llm_tools_root}/global-skills"
    if File.directory?(global_skills_dir) && !llm_tools_covered
      args.push(*rw(global_skills_dir))
    end

    cache_dir = "#{home}/.cache"
    args.push(*rw(cache_dir))    if File.directory?(cache_dir)
    pictures_dir = "#{home}/Pictures"
    args.push(*ro(pictures_dir)) if File.directory?(pictures_dir)
    args.push(*rw(project_dir))
    bind_git_metadata!(args, project_dir, writable: git_rw)

    # Read .sandbox-mounts here rather than where it is applied further down: the
    # symlink check immediately below has to know what the project declared. The
    # mounts themselves are still pushed at the original point, so bwrap's
    # last-mount-wins ordering is untouched.
    declared_mounts = parse_sandbox_mounts(project_dir, home)

    # Symlinks leaving the project are reported, never mounted.
    #
    # They used to be bind-mounted rw automatically. That left .sandbox-mounts as
    # the only project-controlled input we validated, while a symlink — which an
    # agent can create just as easily, which needs no read access to its target, and
    # which git hides behind any ignore pattern lacking a trailing slash — granted
    # the same access with no check at all. Declaring them is the point: a grant you
    # can see in a diff. `sandbox-agent doctor` prints the lines to add.
    #
    # Note what this does and does not do: it withholds the mount the symlink used
    # to earn, it does not hide the target. Somewhere like ~/.local/share/... stays
    # readable through the blanket ro ~/.local mount — the link just stops silently
    # upgrading it to rw. Targets with no other mount covering them (~/.ssh and the
    # rest of the floor, under the tmpfs home) are simply not there.
    project_escaping_symlinks(project_dir, declared_mounts).reject { |s| s[:declared] }.each do |s|
      warn "Undeclared symlink target, not mounted: #{s[:name]} -> #{s[:target]}"
      warn "  declare it in .sandbox-mounts (rw: or ro:), or run: sandbox-agent doctor #{project_dir}"
    end

    # nvm (current Node version)
    if nvm_bin
      nvm_node_dir = File.dirname(nvm_bin) # e.g. .nvm/versions/node/v22.19.0
      args.push(*ro(nvm_node_dir)) if File.directory?(nvm_node_dir)
    end

    args.push(*ro(password_store), *ro(gnupg_dir), *ro(gpg_agent_dir))

    # --- Project-specific extra mounts (.sandbox-mounts) ---
    #
    # Parsed further up (the symlink check needs the declarations); applied here so
    # these land after the auto-mounts they are meant to override — a project's
    # `rw:~/.local/state/cabal` has to win over the blanket ro ~/.local above.
    #
    # The file lives in the project dir, which the agent can write to, so a
    # declaration is a request, not a grant: parse_sandbox_mounts drops anything the
    # floor forbids before we get here.
    extra_path_dirs = []
    declared_mounts.each do |mount|
      path = mount[:path]
      case mount[:mode]
      when "ro"   then args.push(*ro(path)) if File.exist?(path)
      when "rw"   then bind_rw_mount!(args, path, git_writable: git_rw) if File.exist?(path)
      when "path" then extra_path_dirs << path
      else warn "Unknown mount mode '#{mount[:mode]}' in .sandbox-mounts: #{mount[:line]}"
      end
    end

    # Operator-supplied --mount, after the project's own so it wins a conflict, and
    # still before the masks below so the floor wins over both.
    extra_mounts.each do |spec|
      mode, src, dest = spec.split(":", 3)
      abort "Bad --mount (want MODE:SRC or MODE:SRC:DEST): #{spec}" if src.nil? || src.strip.empty?

      src = File.expand_path(src.strip)
      resolved = begin File.realpath(src) rescue src end
      abort "--mount source does not exist: #{src}" unless File.exist?(src)

      # A DEST relocates the mount inside the sandbox. The floor is checked against
      # the host source only — putting a private directory at ~/.config/orca *inside*
      # is exactly how a sandboxed `orca serve` gets a userData of its own, and has
      # nothing to do with reaching the host's.
      dest = dest.nil? || dest.strip.empty? ? src : File.expand_path(dest.strip)

      case mode
      when "ro" then args.push(*ro(src, dest))
      when "rw"
        # Relocated mounts skip the .git split: they are state directories, not
        # checkouts, and bind_git_metadata! resolves paths on the host side.
        dest == src ? bind_rw_mount!(args, src, git_writable: git_rw) : args.push(*rw(src, dest))
      else abort "Bad --mount mode '#{mode}' (want ro or rw): #{spec}"
      end
    end


    # --- no SSH, last word ---
    #
    # gpg-agent doubles as an ssh agent, and its socket dir has to be mounted for
    # commit signing / pass — so mask that one socket. The client binaries go too:
    # with no keys and no agent they are already toothless, but masking them keeps
    # the rule simple (there is no ssh in here). /bin and /usr/bin are bound
    # separately above, hence both paths. Placed after .sandbox-mounts on purpose:
    # in bwrap the last mount wins, so nothing above can undo this.
    ssh_masks = %w[ssh scp sftp ssh-add ssh-agent ssh-keygen ssh-keyscan].flat_map do |name|
      %w[/usr/bin /bin /usr/local/bin].map { |dir| File.join(dir, name) }
    end
    ssh_masks << "#{gpg_agent_dir}/S.gpg-agent.ssh"
    ssh_masks.each { |path| args.push(*mask(path)) if File.exist?(path) }

    args.push("--unshare-pid", "--die-with-parent", "--chdir", project_dir)

    colorterm = ENV.fetch("COLORTERM", "truecolor")
    lang      = ENV.fetch("LANG", "en_US.UTF-8")

    path_dirs = [
      "#{home}/.local/bin",
      *extra_path_dirs,
      *(nvm_bin ? [nvm_bin] : []),
      *(File.directory?(moon_dir) ? ["#{moon_dir}/bin"] : []),
      *(File.directory?("#{kimi_code_dir}/bin") ? ["#{kimi_code_dir}/bin"] : []),
      *(File.directory?("#{grok_dir}/bin") ? ["#{grok_dir}/bin"] : []),
      *(File.directory?("#{asdf_dir}/shims") ? ["#{asdf_dir}/shims"] : []),
      *(go_paths ? [go_paths[:root], go_paths[:path]].compact.map { |d| "#{d}/bin" } : []),
      *(File.directory?("#{bun_dir}/bin") ? ["#{bun_dir}/bin"] : []),
      "/usr/local/bin", "/usr/bin", "/bin",
    ].compact

    bind_command_symlink_target!(args, command, path_dirs) if command

    args.push("--clearenv",
              "--setenv", "HOME", home,
              "--setenv", "PATH", path_dirs.join(":"),
              "--setenv", "TERM", term,
              "--setenv", "COLORTERM", colorterm,
              "--setenv", "LANG", lang,
              *(ENV["EDITOR"] ? ["--setenv", "EDITOR", ENV["EDITOR"]] : []),
              *(!git_rw ? ["--setenv", "GIT_OPTIONAL_LOCKS", "0"] : []),
              *(ENV["DISPLAY"] && !headless ? ["--setenv", "DISPLAY", ENV["DISPLAY"]] : []),
              *(xauthority ? ["--setenv", "XAUTHORITY", xauthority] : []),
              *(ENV["DISABLE_AUTO_COMPACT"] ? ["--setenv", "DISABLE_AUTO_COMPACT", "1"] : []),
              *(ENV["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] ? ["--setenv", "CLAUDE_CODE_DISABLE_AUTO_MEMORY", ENV["CLAUDE_CODE_DISABLE_AUTO_MEMORY"]] : []),
              *(ENV["XDG_RUNTIME_DIR"] ? ["--setenv", "XDG_RUNTIME_DIR", ENV["XDG_RUNTIME_DIR"]] : []),
              *(ENV["ASDF_DATA_DIR"] ? ["--setenv", "ASDF_DATA_DIR", ENV["ASDF_DATA_DIR"]] : []),
              *(ENV["GROK_HOME"] ? ["--setenv", "GROK_HOME", ENV["GROK_HOME"]] : []),
              *(ENV["PRIME_AGENT_CODING_AGENT_DIR"] ? ["--setenv", "PRIME_AGENT_CODING_AGENT_DIR", ENV["PRIME_AGENT_CODING_AGENT_DIR"]] : []),
              *(ENV["GH_TOKEN"] ? ["--setenv", "GH_TOKEN", ENV["GH_TOKEN"]] : []),
              *extra_env.flat_map do |pair|
                key, value = pair.split("=", 2)
                abort "Bad --setenv (want KEY=VALUE): #{pair}" if value.nil? || key.empty?
                ["--setenv", key, value]
              end)


    args
  end


  # Run bwrap as a child instead of exec()ing it. exec() replaces this process
  # image, so no ensure hook fires and a caller's temp dir — with a live
  # multiplexer socket in it — is left behind on every launch.
  #
  # on_line     called with each stdout line, which is passed through unchanged.
  #             Lets a caller spot a server's startup JSON without parsing a log.
  # stop_on_int true when Ctrl-C must stop the sandboxed process. Not the default,
  #             because the sandboxed process is PID 1 in its own namespace and the
  #             kernel drops default-action signals to PID 1 unless that process
  #             installed a handler — Electron has not, so SIGINT reaches it and
  #             does nothing. TERM bwrap instead and let the namespace teardown
  #             take the child with it. An interactive multiplexer wants the
  #             opposite: it handles its own keys, so ignore INT here.
  def spawn_sandbox(args, cleanup_dir: nil, on_line: nil, stop_on_int: false)
    status = nil
    begin
      if on_line
        reader, writer = IO.pipe
        # pgroup: true puts bwrap in its own process group, so a terminal's
        # Ctrl-C does NOT reach it directly. That matters: taking a bare SIGINT,
        # bwrap goes away without tearing the sandbox down with it, and the
        # server is left listening and orphaned. Routed through the INT trap
        # below it gets an orderly TERM instead — the same path `--stop` takes,
        # which is exactly why --stop worked while Ctrl-C did not.
        pid = spawn("bwrap", *args, out: writer, pgroup: true)
        writer.close
        Thread.new do
          reader.each_line do |line|
            print line
            $stdout.flush
            on_line.call(line)
          end
        end
      else
        pid = spawn("bwrap", *args)
      end

      if stop_on_int
        # TERM then KILL: TERM alone is not reliably enough, because the
        # sandboxed process is PID 1 in its namespace and the kernel drops
        # default-action signals to PID 1 unless that process installed a
        # handler. Killing bwrap outright tears the namespace down, which takes
        # everything inside it.
        trap("INT") do
          Thread.new do
            # Say so: the graceful window below is up to 5s of silence, and a
            # server that looks hung after Ctrl-C reads exactly like one that
            # ignored it.
            $stderr.puts "\nStopping..."
            Process.kill("TERM", pid) rescue nil
            sleep 5
            Process.kill("KILL", pid) rescue nil
          end
        end
      else
        trap("INT", "IGNORE")
      end
      trap("TERM") { Process.kill("TERM", pid) rescue nil }
      _, status = Process.waitpid2(pid)
    ensure
      # Only ever a directory the caller created, never a glob.
      FileUtils.remove_entry(cleanup_dir) if cleanup_dir && File.directory?(cleanup_dir)
    end

    status.exitstatus || (status.termsig ? 128 + status.termsig : 1)
  end

end
