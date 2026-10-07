#!/usr/bin/env ruby

# Everything X11 about the sandbox: keeping the host display out, the private
# Xephyr screen each session gets instead, and the clipboard bridge that
# replaces the direct access the sandbox used to have.

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
  # on an abstract socket (@/tmp/.X11-unix/X0). Network namespace isolation
  # blocks that socket; access control is kept as an additional barrier.
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

  # --- Clipboard relay ------------------------------------------------------
  #
  # This child stays on the host. It owns a CLIPBOARD proxy on the private
  # screen and fetches bytes only when a client asks to paste (and vice versa).
  # No content cache, no clipboard socket, no special xclip on the sandbox PATH.
  def start_clipboard(screen)
    return unless ENV["DISPLAY"]

    _, status = Open3.capture2e("/usr/bin/python3", "-I", "-B", "-c", "import Xlib")
    abort "Clipboard relay needs python3-xlib. Install it with: sudo apt install python3-xlib" unless status.success?

    reader, writer = IO.pipe
    pid = Process.spawn("/usr/bin/python3", "-I", "-B", File.join(__dir__, "sandbox-clipboard.py"),
                        screen.display, screen.auth, out: writer, rlimit_core: 0)
    writer.close
    ready = IO.select([reader], nil, nil, 5) && reader.gets == "ready\n"
    reader.close
    unless ready
      stop_clipboard(pid)
      abort "Clipboard relay did not start; refusing to launch a session with a broken clipboard."
    end
    pid
  rescue SystemCallError => e
    abort "Cannot start clipboard relay: #{e.message}"
  end

  def stop_clipboard(pid)
    Process.kill("TERM", pid) rescue nil
    Process.waitpid(pid) rescue nil
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
    xauth_entry(cookie)
  end

  def xauth_entry(cookie, family: 0xFFFF, address: "", number: "")
    field = ->(bytes) { [bytes.bytesize].pack("n") + bytes.b }
    [family].pack("n") + field.call(address) + field.call(number) + field.call("MIT-MAGIC-COOKIE-1") + field.call(cookie)
  end

  def start_screen(socket_dir, title, gui: true)
    # Every session has its own cookie as well as an isolated network
    # namespace, so an agent cannot connect to another session's screen.
    auth = File.join(socket_dir, "xephyr.auth")
    cookie = SecureRandom.random_bytes(16)
    File.binwrite(auth, xauth_wildcard_entry(cookie))
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
    server = gui ? "Xephyr" : "Xvfb"
    log = File.join(socket_dir, gui ? "xephyr.log" : "xvfb.log")
    reader, writer = IO.pipe
    options = gui ? ["-title", title, "-name", token, "-screen", SCREEN_SIZE, "-resizeable",
                     "-br", "-no-host-grab"] : ["-screen", "0", "#{SCREEN_SIZE}x24"]
    pid = Process.spawn(server, "-displayfd", writer.fileno.to_s, "-auth", auth,
                        *options, "-noreset", "-nolisten", "tcp",
                        writer.fileno => writer, out: log, err: log)
    writer.close
    number = IO.select([reader], nil, nil, 10) && reader.gets.to_s.strip
    reader.close
    unless number.to_s.match?(/\A\d+\z/)
      Process.kill("TERM", pid) rescue nil
      warn "Private X server #{server} did not come up (see #{log})."
      return nil
    end

    # python-xlib does not match wildcard cookies. Keep that server record and
    # add a numbered FamilyLocal record for the host-side relay's connection.
    File.open(auth, "ab") do |file|
      file.write(xauth_entry(cookie, family: 256, address: Socket.gethostname, number: number))
    end

    window = find_window(token) if gui
    if window
      name_window(window, title)
      stow_window(window)
    end
    Screen.new(display: ":#{number}", pid: pid, auth: auth, window: window)
  rescue SystemCallError => e
    warn "Cannot start private X server (#{e.message}). Install xserver-xephyr and xvfb."
    nil
  end

  def stop_screen(screen)
    Process.kill("TERM", screen.pid) rescue nil
    Process.waitpid(screen.pid) rescue nil
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
    fit = { normal: {}, size: nil }
    Thread.new do
      loop do
        sleep 0.5
        break if (Process.waitpid(screen.pid, Process::WNOHANG) rescue true)

        fit_windows(env, fit)
      rescue StandardError
        nil
      end
    end
  end

  # Xephyr has no window manager, so nothing resizes Chrome when you resize
  # the Xephyr window: the screen changes and Chrome stays put, cut off. Keep
  # every normal top-level window filling the screen instead, as a maximizing
  # WM would. Menus and popups are override-redirect and are left alone.
  def fit_windows(env, fit)
    width, height = root_size(env)
    return unless width

    announce_size(env, width, height) if fit[:size] != [width, height]
    fit[:size] = [width, height]
    normal = fit[:normal]

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

  # When Xephyr follows its window to a new size, Chrome is not told: it keeps
  # the screen size it started with and clamps every click to it, so anything
  # past the old right or bottom edge cannot be clicked. Setting the same size
  # again through RandR sends the notification Chrome listens for.
  def announce_size(env, width, height)
    system(env, "xrandr", "-s", "#{width}x#{height}", out: File::NULL, err: File::NULL)
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

end
