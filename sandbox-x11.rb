#!/usr/bin/env ruby

# Everything X11 about the sandbox: keeping the host display out, the private
# Xephyr screen each session gets instead, and the clipboard bridge that
# replaces the direct access the sandbox used to have.
#
# Also loaded from inside the sandbox by sandbox-shims/xclip, under the system
# Ruby (3.0) — keep it stdlib-only and 3.0-compatible.

require "digest"
require "etc"
require "open3"
require "securerandom"
require "socket"

module SandboxX11
  module_function

  # --- The host display stays out -------------------------------------------
  #
  # A client connected to the host X server can read every keystroke, capture
  # the screen, and type into a host terminal (XTEST) — a full sandbox escape.
  # Not mounting /tmp/.X11-unix is not enough to keep it out: Xorg also listens
  # on an abstract socket (@/tmp/.X11-unix/X0), and abstract sockets belong to
  # the network namespace, which the sandbox shares with the host.
  #
  # What keeps the sandbox out is then the cookie it is never given — unless the
  # server waives it. GNOME does exactly that at every login (xhost
  # +SI:localuser:<you>): any process of this uid gets in, cookie or not. So the
  # rule is revoked before every launch. Host apps do not notice; they present
  # the cookie.
  def close_host_display!
    return unless ENV["DISPLAY"]

    listing = xhost!
    if access_control_disabled?(listing)
      xhost!("-")
      warn "Re-enabled access control on the host X server: it admitted any client."
    end

    user = Etc.getpwuid(Process.uid).name
    if localuser_grant?(listing, user)
      rule = "SI:localuser:#{user}"
      xhost!("-#{rule}")
      warn "Revoked #{rule} on the host X server so sandboxes cannot reach it (undo: xhost +#{rule})."
    end
    add_numbered_cookie
  end

  # Without the localuser rule, a host app needs the cookie, and some cannot
  # read the one GDM writes. GDM leaves the display number empty, which libXau
  # takes to mean any display; Perl's X11::Auth (Shutter) wants the number
  # spelled out, finds nothing, and was only ever let in by the rule. Give it
  # the same cookie under an explicit number. GDM rewrites the file at each
  # login, hence once per launch, like the revocation.
  def add_numbered_cookie
    number = ENV["DISPLAY"].to_s[/\A(?:unix)?:(\d+)/, 1]
    file = ENV["XAUTHORITY"]
    return unless number && file && File.file?(file)

    listing, status = Open3.capture2("xauth", "-f", file, "list", err: File::NULL)
    entry = status.success? && missing_numbered_cookie(listing, Socket.gethostname, number)
    system("xauth", "-f", file, "add", *entry, out: File::NULL, err: File::NULL) if entry
  rescue SystemCallError
    nil
  end

  def missing_numbered_cookie(xauth_list, host, number)
    entries = xauth_list.lines.map(&:split)
    return nil if entries.any? { |name, _| name == "#{host}/unix:#{number}" }

    _, protocol, cookie = entries.find { |name, proto, _| name == "#{host}/unix:" && proto == "MIT-MAGIC-COOKIE-1" }
    cookie && ["#{host}/unix:#{number}", protocol, cookie]
  end

  def xhost!(*args)
    out, status = Open3.capture2e("xhost", *args)
    abort "xhost #{args.join(" ")} failed: #{out.strip}" unless status.success?
    out
  rescue SystemCallError
    abort "xhost not found: cannot make sure the host X server refuses sandboxes. Install x11-xserver-utils."
  end

  def localuser_grant?(xhost_output, user)
    xhost_output.lines.any? { |line| line.strip.casecmp?("SI:localuser:#{user}") }
  end

  def access_control_disabled?(xhost_output)
    xhost_output.start_with?("access control disabled")
  end

  # --- Clipboard bridge -----------------------------------------------------
  #
  # The sandbox reads and writes the host CLIPBOARD through sandbox-agent, over
  # a socket in the session dir, never through the host X server. Claude Code's
  # Ctrl+V shells out to `xclip`, so a stand-in xclip (sandbox-shims/xclip) is
  # first on the sandbox PATH and speaks this protocol:
  #
  #   READ <target>\n             -> the clipboard content, empty if none
  #   WRITE <target>\n<bytes...>  -> sets the clipboard, empty reply
  #
  # PRIMARY is not bridged. That is deliberate: it is where `pass` can be told
  # to put passwords (PASSWORD_STORE_X_SELECTION=primary) to keep them out.

  CLIPBOARD_MAX_BYTES = 64 * 1024 * 1024

  # Selection target names: image/png, UTF8_STRING, text/plain;charset=utf-8...
  TARGET = %r{\A[\w.+/;=-]+\z}

  # What to carry between the host and Xephyr, best first. Text before images:
  # a spreadsheet copy offers both, and the text is what was meant.
  TEXT_TARGETS  = ["UTF8_STRING", "text/plain;charset=utf-8", "STRING", "text/plain"].freeze
  IMAGE_TARGETS = %w[image/png image/jpeg image/gif image/webp image/bmp].freeze

  XCLIP_OPTIONS = %w[-display -filter -help -in -loops -noutf8 -out -quiet -rmlastnl
                     -selection -sensitive -silent -target -verbose -version].freeze
  XCLIP_VALUED  = %w[-display -loops -selection -target].freeze

  # Like xclip itself: an option may be abbreviated while it stays unambiguous.
  def xclip_option(arg)
    return arg if XCLIP_OPTIONS.include?(arg)

    matches = XCLIP_OPTIONS.select { |opt| opt.start_with?(arg) }
    matches.size == 1 && arg.length >= 2 ? matches.first : nil
  end

  # An `xclip` command line as the bridge sees it, or nil when it is not a
  # CLIPBOARD read/write. Defaults follow xclip: write mode, UTF8_STRING, and
  # the PRIMARY selection — which is not bridged, so a call has to name the
  # clipboard.
  def xclip_request(argv)
    request = { mode: :write, target: "UTF8_STRING", files: [], rmlastnl: false, filter: false }
    selection = "primary"
    args = argv.dup

    until args.empty?
      arg = args.shift
      unless arg.start_with?("-")
        request[:files] << arg
        next
      end

      option = xclip_option(arg) or return nil
      value = (args.shift or return nil) if XCLIP_VALUED.include?(option)

      case option
      when "-in"        then request[:mode] = :write
      when "-out"       then request[:mode] = :read
      when "-selection" then selection = value
      when "-target"    then request[:target] = value
      when "-rmlastnl"  then request[:rmlastnl] = true
      when "-filter"    then request[:filter] = true
      when "-help", "-version" then return nil
      end
      # -display, -loops, -noutf8, -quiet, -silent, -verbose, -sensitive have no
      # bearing on what crosses the bridge.
    end

    # xclip itself only looks at the first letter of the selection name.
    return nil unless selection.start_with?("c") && request[:target].match?(TARGET)

    request
  end

  def parse_bridge_request(line)
    verb, target, extra = line.to_s.chomp.split(" ", 3)
    return nil if extra || target.nil? || !target.match?(TARGET)

    mode = { "READ" => :read, "WRITE" => :write }[verb]
    mode && [mode, target]
  end

  # The best target on offer, in the owner's own spelling (charset case varies).
  def pick_target(targets)
    (TEXT_TARGETS + IMAGE_TARGETS).each do |wanted|
      found = targets.find { |t| t.casecmp?(wanted) }
      return found if found
    end
    nil
  end

  # env selects the X server: {} is the host one (sandbox-agent runs there).
  # `timeout`: a selection owner that never answers must not hang the bridge.
  def read_clipboard(target, env = {})
    out, _err, status = Open3.capture3(env, "timeout", "5", "xclip", "-o", "-selection", "clipboard",
                                       "-t", target, binmode: true)
    status.success? && !out.empty? && out.bytesize <= CLIPBOARD_MAX_BYTES ? out : nil
  rescue SystemCallError
    nil
  end

  def clipboard_targets(env = {})
    read_clipboard("TARGETS", env).to_s.lines(chomp: true)
  end

  # xclip -i forks a child that serves the selection until someone else takes
  # it; the parent exits once stdin is drained.
  def write_clipboard(target, data, env = {})
    reader, writer = IO.pipe
    pid = Process.spawn(env, "xclip", "-i", "-selection", "clipboard", "-t", target,
                        in: reader, out: File::NULL, err: File::NULL)
    reader.close
    writer.binmode
    writer.write(data)
    writer.close
    Process.detach(pid)
  rescue SystemCallError
    nil
  end

  def serve_clipboard(socket_path)
    server = UNIXServer.new(socket_path)
    Thread.new do
      loop do
        Thread.new(server.accept) do |client|
          handle_bridge_client(client)
        rescue StandardError
          nil # a bad request costs that request, never the session
        ensure
          client.close
        end
      end
    end
  end

  def handle_bridge_client(client)
    client.binmode
    mode, target = parse_bridge_request(client.gets(256))
    case mode
    when :read
      data = read_clipboard(target)
      client.write(data) if data
    when :write
      data = client.read(CLIPBOARD_MAX_BYTES + 1).to_s
      write_clipboard(target, data) if data.bytesize <= CLIPBOARD_MAX_BYTES
    end
  end

  # Client side, used by the stand-in xclip inside the sandbox.
  def bridge_call(socket_path, line, data = nil)
    UNIXSocket.open(socket_path) do |sock|
      sock.binmode
      sock.write(line)
      sock.write(data) if data
      sock.close_write
      sock.read
    end
  end

  # --- Private screen (Xephyr) ----------------------------------------------
  #
  # The sandbox gets an X server of its own, shown as one window on the host
  # desktop: Chrome launched by an agent appears in it, and you can click in it
  # (Miriad's pick.js), but nothing in it can see or reach the host's windows.
  # Xephyr runs on the host as an ordinary client of the host X server; only
  # its socket goes into the sandbox.

  SCREEN_SIZE = "1600x900"

  Screen = Struct.new(:display, :pid, :auth, :window, keyword_init: true)

  # One Xauthority record that matches any display number. The server learns
  # the cookie through -auth before its display number is known (-displayfd
  # picks it), and the client finds it through XAUTHORITY.
  def xauth_wildcard_entry(cookie)
    field = ->(bytes) { [bytes.bytesize].pack("n") + bytes.b }
    [0xFFFF].pack("n") + field.call("") + field.call("") + field.call("MIT-MAGIC-COOKIE-1") + field.call(cookie)
  end

  def start_screen(socket_dir, title)
    # Every session has its own cookie: sandboxes share the network namespace,
    # so each can reach every Xephyr's abstract socket. The cookie is what
    # keeps one agent off another's screen.
    auth = File.join(socket_dir, "xephyr.auth")
    File.binwrite(auth, xauth_wildcard_entry(SecureRandom.random_bytes(16)))
    File.chmod(0o600, auth)

    # -name sets only the res_name half of WM_CLASS (res_class stays "Xephyr",
    # hard-coded). A random one finds this exact window afterwards, even with
    # two sessions on one project; name_window then sets the real name.
    token = "sandbox-agent-#{SecureRandom.hex(4)}"
    # -noreset: by default Xephyr regenerates the whole server each time its
    # last client leaves. Until Chrome is up, every watch_screen poll is that
    # last client, so it reset several times a second, and one of those resets
    # eventually took it down, window and all, mid-session.
    # The log is there so that the next death has a cause.
    log = File.join(socket_dir, "xephyr.log")
    reader, writer = IO.pipe
    pid = Process.spawn("Xephyr", "-displayfd", writer.fileno.to_s, "-auth", auth,
                        "-title", title, "-name", token, "-screen", SCREEN_SIZE, "-resizeable",
                        "-br", "-no-host-grab", "-noreset", "-nolisten", "tcp",
                        writer.fileno => writer, out: log, err: log)
    writer.close
    number = IO.select([reader], nil, nil, 10) && reader.gets.to_s.strip
    reader.close
    unless number.to_s.match?(/\A\d+\z/)
      Process.kill("TERM", pid) rescue nil
      warn "No GUI: Xephyr did not come up. Pass --no-gui to skip it."
      return nil
    end

    window = find_window(token)
    if window
      name_window(window, title)
      stow_window(window)
    end
    Screen.new(display: ":#{number}", pid: pid, auth: auth, window: window)
  rescue SystemCallError => e
    warn "No GUI: cannot start Xephyr (#{e.message}). Install xserver-xephyr, or pass --no-gui."
    nil
  end

  def stop_screen(screen)
    Process.kill("TERM", screen.pid)
  rescue SystemCallError
    nil
  end

  def screen_env(screen) = { "DISPLAY" => screen.display, "XAUTHORITY" => screen.auth }

  # --onlyvisible: the window must be mapped before stow_window, or the window
  # manager ignores the minimize request and maps it in plain view anyway.
  def find_window(token)
    20.times do
      out, = Open3.capture2("xdotool", "search", "--onlyvisible", "--classname", "^#{token}$", err: File::NULL)
      id = out.split.first
      return id if id
      sleep 0.25
    end
    nil
  rescue SystemCallError
    nil
  end

  # The dock labels a window by its WM_CLASS, which Xephyr hard-codes: without
  # this, every session shows up as an identical "Xephyr".
  def name_window(window, title)
    system("xdotool", "set_window", "--classname", title, "--class", title, window,
           out: File::NULL, err: File::NULL)
  end

  # A new window grabs the screen and the focus at every launch, though most
  # sessions never need it. Send it to the last workspace, minimized: it stays
  # in the dock under the session's name, for when it is wanted.
  def stow_window(window)
    count, = Open3.capture2("xdotool", "get_num_desktops", err: File::NULL)
    last = count.to_i - 1
    if last.positive?
      system("xdotool", "set_desktop_for_window", window, last.to_s, out: File::NULL, err: File::NULL)
    end
    system("xdotool", "windowminimize", window, out: File::NULL, err: File::NULL)
  end

  # Polls rather than listens: there is no X event API in stdlib Ruby, and two
  # or three small commands every half second are cheap. Ends with Xephyr —
  # closing its window loses the screen for the rest of the session.
  def watch_screen(screen)
    env = screen_env(screen)
    normal = {}
    clipboard = { focused: false, carried: nil }
    Thread.new do
      loop do
        sleep 0.5
        break if (Process.waitpid(screen.pid, Process::WNOHANG) rescue true)

        fit_windows(env, normal)
        sync_clipboard_on_focus(screen, env, clipboard) if screen.window
      rescue StandardError
        nil
      end
    end
  end

  # Xephyr has no window manager, so nothing resizes Chrome when you resize
  # the Xephyr window: the screen changes and Chrome stays put, cut off. Keep
  # every normal top-level window filling the screen instead, as a maximizing
  # WM would. Menus and popups are override-redirect and are left alone.
  def fit_windows(env, normal)
    width, height = root_size(env)
    return unless width

    out, = Open3.capture2(env, "xdotool", "search", "--onlyvisible", "--maxdepth", "1", "--name", ".",
                          "getwindowgeometry", "--shell", "%@", err: File::NULL)
    out.split(/(?=^WINDOW=)/).each do |block|
      geometry = block.scan(/^(\w+)=(\d+)$/).to_h
      id = geometry["WINDOW"] or next
      normal[id] = normal_window?(env, id) unless normal.key?(id)
      next unless normal[id]
      next if geometry.values_at("X", "Y", "WIDTH", "HEIGHT") == ["0", "0", width.to_s, height.to_s]

      system(env, "xdotool", "windowmove", id, "0", "0", "windowsize", id, width.to_s, height.to_s,
             out: File::NULL, err: File::NULL)
    end
  end

  # `xdotool getdisplaygeometry` goes stale after a resize; xwininfo does not.
  def root_size(env)
    out, = Open3.capture2(env, "xwininfo", "-root", err: File::NULL)
    width = out[/^\s*Width:\s*(\d+)/, 1]
    height = out[/^\s*Height:\s*(\d+)/, 1]
    width && height ? [width.to_i, height.to_i] : nil
  end

  def normal_window?(env, id)
    info, = Open3.capture2(env, "xwininfo", "-id", id, "-all", err: File::NULL)
    type, = Open3.capture2(env, "xprop", "-id", id, "_NET_WM_WINDOW_TYPE", err: File::NULL)
    info.include?("Override Redirect State: no") && type.include?("_NET_WM_WINDOW_TYPE_NORMAL")
  end

  # Xephyr's clipboard is its own, so Chrome in it cannot see what you copied
  # on the desktop. Carry it across when focus crosses: into the Xephyr window,
  # the host clipboard follows you in; back out, whatever was copied inside
  # comes along. One format each way — the best pick_target finds.
  def sync_clipboard_on_focus(screen, env, state)
    active, = Open3.capture2("xdotool", "getactivewindow", err: File::NULL)
    focused = active.strip == screen.window
    return if focused == state[:focused]

    state[:focused] = focused
    focused ? carry_clipboard({}, env, state) : carry_clipboard(env, {}, state)
  end

  # The digest stops the ping-pong: what was just carried one way is still the
  # clipboard's content on the next crossing, and must not be carried back.
  def carry_clipboard(from, to, state)
    target = pick_target(clipboard_targets(from)) or return
    data = read_clipboard(target, from) or return
    digest = Digest::SHA256.digest(data)
    return if digest == state[:carried]

    write_clipboard(target, data, to)
    state[:carried] = digest
  end
end
