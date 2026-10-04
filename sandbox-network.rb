# A private network for each sandbox (bwrap --unshare-net), and the few doors
# out of it.
#
# Why: sharing the host's network namespace handed a sandbox everything the host
# can reach — production servers, an ssh tunnel opened on localhost and long
# forgotten, abstract unix sockets (session buses among them) that no file mount
# can hide. With its own namespace the sandbox has a loopback and nothing else.
#
# The doors, all opened by sandbox-agent on the host and all through socket files
# in the session's private socket dir (a socket file crosses the namespace
# boundary, a network does not):
#
#   proxy     HTTP(S) proxy, seen inside as 127.0.0.1:3128 through HTTP(S)_PROXY.
#             Runs here, on the host, so it decides with the host's DNS and
#             logs every request. Lets through only allowlisted hosts: the
#             global ~/.sandbox-agent/internet-allowlist plus the
#             project's internet: lines, both re-read at every request, so
#             `sandbox-agent allow` opens a host in a running session. Never host-local addresses
#             (loopback, link-local) unless declared with host:.
#   host:     a host service the sandbox may reach, declared in .sandbox-mounts
#             (approved like mounts): the relay inside listens on the same
#             ip:port and tunnels each connection through the proxy.
#   expose:   a dev server inside that the host must reach (your own browser):
#             the host listens on ip:port and hands each connection in.
#
# Programs that ignore HTTP(S)_PROXY cannot get out at all: they fail with
# "network unreachable" or a DNS error. That is the price, paid in plain sight.
#
# Not supported, on purpose: IDE integration over localhost — see the comment
# in sandbox-lib.rb.

require "digest"
require "fileutils"
require "ipaddr"
require "json"
require "open3"
require "socket"
require "uri"

module SandboxNetwork
  module_function

  PROXY_LISTEN = ["127.0.0.1", 3128].freeze
  RELAY = File.join(__dir__, "sandbox-network-relay")
  ALLOW_FILE = File.expand_path("~/.sandbox-agent/internet-allowlist")

  # Loopback names and the reserved dev TLDs stay inside the sandbox: they mean
  # the sandbox's own servers, never something for the proxy to fetch.
  NO_PROXY = "localhost,127.0.0.1,::1,127.0.0.0/8,.localhost,.test"

  # A range expands to one listener per address and port; keep that sane.
  MAX_ENDPOINTS = 1024

  # "127.1.0.1:3000,8080" or "127.5.0.0/27:8000" -> [[ip, port], ...]
  def parse_endpoints(spec)
    m = spec.to_s.strip.match(%r{\A(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?:(\d+(?:,\d+)*)\z})
    raise ArgumentError, "want IP[/PREFIX]:PORT[,PORT...], got #{spec.inspect}" unless m

    ip, prefix, ports = m.captures
    ports = ports.split(",").map(&:to_i)
    raise ArgumentError, "bad port in #{spec.inspect}" unless ports.all? { |p| p.between?(1, 65_535) }

    raise ArgumentError, "bad prefix in #{spec.inspect}" if prefix && prefix.to_i > 32
    size = prefix ? 2**(32 - prefix.to_i) : 1
    if size * ports.size > MAX_ENDPOINTS
      raise ArgumentError, "#{spec} expands to #{size * ports.size} endpoints (max #{MAX_ENDPOINTS})"
    end

    addrs =
      if prefix
        range = IPAddr.new("#{ip}/#{prefix}").to_range.map(&:to_s)
        prefix.to_i < 31 ? range[1..-2] : range # no network or broadcast address
      else
        IPAddr.new(ip).to_s
        [ip]
      end
    addrs.product(ports)
  rescue IPAddr::InvalidAddressError
    raise ArgumentError, "bad address in #{spec.inspect}"
  end

  def resolve(name)
    Addrinfo.getaddrinfo(name, nil, nil, :STREAM).map(&:ip_address).uniq
  rescue SocketError
    []
  end

  # The allowlist: ~/.sandbox-agent/internet-allowlist plus a project's
  # internet: lines.
  #
  #   example.org      the name and every subdomain (*.example.org: the same)
  #   203.0.113.7      an address, for a host with no name
  #   198.51.100.0/24  a range
  #
  # Names are matched as asked for, never resolved: allowing a name must not
  # open every other site that shares its address.
  class HostList
    def self.parse(text)
      names = []
      nets = []
      text.each_line do |raw|
        entry = raw.sub(/#.*/, "").strip.downcase
        next if entry.empty?

        begin
          nets << [entry, IPAddr.new(entry)]
        rescue IPAddr::InvalidAddressError
          names << [entry, entry.delete_prefix("*.")]
        end
      end
      new(names, nets)
    end

    def initialize(names, nets)
      @names = names
      @nets = nets
    end

    # The entry that matches, or nil.
    def match(host, addrs)
      host = host.to_s.downcase
      hit = @names.find { |_, name| host == name || host.end_with?(".#{name}") }
      return hit.first if hit

      addrs.each do |a|
        ip = IPAddr.new(a)
        hit = @nets.find { |_, net| net.family == ip.family && net.include?(ip) }
        return hit.first if hit
      end
      nil
    end
  end

  def host_local?(ip)
    ip = ip.native
    ip.loopback? || ip.link_local? || ip.to_s == "0.0.0.0" || ip.to_s == "::"
  end

  # [:allow | :deny, reason].
  def verdict(host, port, addrs, to_host, allow)
    return [:allow, "declared"] if addrs.any? { |a| to_host.include?([a, port]) }

    local = addrs.find { |a| host_local?(IPAddr.new(a)) }
    return [:deny, "#{local} is local to the host; declare it with host: in .sandbox-mounts"] if local
    return [:allow, nil] if allow.match(host, addrs)

    [:deny, "#{host} is not on the allowlist: add internet:#{host} to .sandbox-mounts, " \
            "then ask the user to run `sandbox-agent allow`"]
  end

  # The hosts in a .sandbox-mounts text's internet: lines.
  def internet_hosts(text)
    text.to_s.each_line.filter_map do |line|
      line = line.strip.sub(/\s+#.*\z/, "")
      line.match(/\Ainternet:\s*(\S+)\z/)&.[](1)
    end
  end

  # The allowlist as of now: the global file plus the project's approved internet:
  # lines. Called at every request, so an approval reaches running sessions.
  def allowlist(approved_mounts_text)
    global = File.exist?(ALLOW_FILE) ? File.read(ALLOW_FILE) : ""
    HostList.parse([global, *internet_hosts(approved_mounts_text)].join("\n"))
  end

  # [:connect | :http, host, port], or nil for anything that is not a proxy request.
  def parse_request_head(head)
    method, target, version = head.to_s.lines.first.to_s.split(" ")
    return nil unless version&.start_with?("HTTP/")

    if method == "CONNECT"
      m = target.match(/\A\[([^\]]+)\]:(\d+)\z/) || target.match(/\A([^:\[\]]+):(\d+)\z/)
      m && [:connect, m[1], m[2].to_i]
    elsif target.start_with?("http://")
      uri = URI(target)
      uri.hostname && [:http, uri.hostname, uri.port]
    end
  rescue URI::InvalidURIError
    nil
  end

  # Copy one way until EOF, then pass the EOF on. readpartial, not copy_stream:
  # it hands over what gets() already buffered.
  def pump(from, to)
    loop { to.write(from.readpartial(65_536)) }
  rescue EOFError, IOError, SystemCallError
    nil
  ensure
    begin
      to.close_write
    rescue IOError, SystemCallError
      nil
    end
  end

  def splice(a, b)
    [Thread.new { pump(a, b) }, Thread.new { pump(b, a) }].each(&:join)
  ensure
    [a, b].each { |s| s.close rescue nil }
  end

  # Accept forever on `server`, one thread per connection; a failed connection
  # costs that connection, never the session.
  def serve(server)
    Thread.new do
      loop do
        Thread.new(server.accept) do |client|
          yield client
        rescue StandardError
          nil
        ensure
          client.close rescue nil
        end
      end
    rescue IOError
      nil # server closed: the session is over
    end
  end

  def refuse(client, code, reason)
    client.write("HTTP/1.1 #{code} Sandbox\r\nContent-Type: text/plain\r\nConnection: close\r\n" \
                 "X-Sandbox-Reason: #{reason}\r\n\r\nsandbox-agent proxy: #{reason}\n")
  end

  def handle_proxy_client(client, to_host, allow, log)
    head = client.gets("\r\n\r\n", 65_536)
    kind, host, port = parse_request_head(head)
    return refuse(client, 400, "not a proxy request") unless kind

    addrs = resolve(host)
    if addrs.empty?
      log.call(:deny, host, port, "does not resolve")
      return refuse(client, 502, "#{host} does not resolve")
    end

    action, reason = verdict(host, port, addrs, to_host, allow.call)
    log.call(action, host, port, reason)
    return refuse(client, 403, reason) if action == :deny

    upstream = addrs.lazy.filter_map { |a| TCPSocket.new(a, port, connect_timeout: 10) rescue nil }.first
    return refuse(client, 502, "cannot connect to #{host}:#{port}") unless upstream

    if kind == :connect
      client.write("HTTP/1.1 200 Connection established\r\n\r\n")
    else
      upstream.write(head) # absolute-form request line: origin servers must accept it
    end
    splice(client, upstream)
  end

  # Host side of one sandbox session.
  class Session
    attr_reader :env, :info_path

    # allow: a callable returning the current allowlist (a HostList).
    def initialize(dir, to_host:, expose:, allow:, log:)
      @dir = dir
      @servers = []
      proxy_socket = File.join(dir, "proxy.sock")
      expose_socket = File.join(dir, "expose.sock")

      proxy = UNIXServer.new(proxy_socket)
      @servers << proxy
      SandboxNetwork.serve(proxy) { |c| SandboxNetwork.handle_proxy_client(c, to_host, allow, log) }

      expose.each do |ip, port|
        server = begin
          TCPServer.new(ip, port)
        rescue SystemCallError => e
          warn "expose #{ip}:#{port}: #{e.message} (the host cannot reach it this session)"
          next
        end
        @servers << server
        SandboxNetwork.serve(server) do |client|
          inside = UNIXSocket.new(expose_socket)
          inside.write("#{ip}:#{port}\n")
          SandboxNetwork.splice(client, inside)
        end
      end

      @config = File.join(dir, "network.json")
      File.write(@config, JSON.generate(proxy_socket: proxy_socket, expose_socket: expose_socket,
                                        proxy_listen: PROXY_LISTEN, to_host: to_host, expose: expose))
      proxy_url = "http://#{PROXY_LISTEN.join(':')}"
      @env = { "HTTP_PROXY" => proxy_url, "HTTPS_PROXY" => proxy_url,
               "http_proxy" => proxy_url, "https_proxy" => proxy_url,
               "NO_PROXY" => NO_PROXY, "no_proxy" => NO_PROXY,
               # Node's built-in fetch ignores the variables above unless told
               # otherwise; Node 24 understands this, older ones skip it.
               "NODE_USE_ENV_PROXY" => "1" }
    end

    # The command, started by the relay once its listeners are up.
    def wrap(cmd) = ["/usr/bin/ruby", RELAY, @config, "--", *cmd]

    def stop
      @servers.each { |s| s.close rescue nil }
    end
  end

  def start(dir, to_host:, expose:, allow:, log:)
    Session.new(dir, to_host: to_host, expose: expose, allow: allow, log: log)
  end

  # One log per project, kept across sessions: what to read before turning a
  # host into an allowlist entry. Under ~/.sandbox-agent, which no sandbox
  # mounts: sandbox-agent binds each sandbox its own log read-only, so an
  # agent can read why it was refused, not rewrite the record.
  LOG_DIR = File.expand_path("~/.sandbox-agent/network-log")

  def log_path(project_dir)
    hash = Digest::SHA256.hexdigest(project_dir)[0, 8]
    File.join(LOG_DIR, "#{File.basename(project_dir)}-#{hash}.log")
  end

  def logger(path)
    FileUtils.mkdir_p(File.dirname(path))
    lock = Mutex.new
    lambda do |action, host, port, reason|
      line = "#{Time.now.strftime('%FT%T')} #{action} #{host}:#{port}#{reason ? " (#{reason})" : ''}\n"
      lock.synchronize { File.write(path, line, mode: "a") }
    end
  end

  # What an agent inside needs to make sense of a network failure, at the path
  # in $SANDBOX_NETWORK_INFO.
  def info_text(host_lines:, expose_lines:, log_path:)
    list = ->(lines) { lines.empty? ? "  (none)\n" : lines.map { |l| "  #{l}\n" }.join }
    <<~TXT
      This sandbox has a private network: its own loopback, and no route out.

      The only way to the internet is the proxy at http://#{PROXY_LISTEN.join(':')},
      set in HTTP_PROXY / HTTPS_PROXY. Tools that honour those variables work.
      A tool that ignores them fails with "network unreachable", ENETUNREACH,
      ENOTFOUND or another DNS error: that is this sandbox, not an outage. Look
      for the tool's own proxy setting. Node's built-in fetch follows the proxy
      from Node 24 on (NODE_USE_ENV_PROXY=1 is set); Node 22 cannot.

      The proxy only lets through allowlisted hosts. A refused host gets HTTP
      403, and every request, allowed or refused, is logged with its reason in:
        #{log_path}
      To ask for a site, add an internet: line to .sandbox-mounts, e.g.
      internet:guides.rubyonrails.org (a name covers its subdomains), and ask the
      user to run `sandbox-agent allow`. It works at once, no restart. You
      cannot approve it yourself. The host's own addresses (loopback,
      link-local) are never reachable unless declared with host:.

      Host services this sandbox may reach (host: lines in .sandbox-mounts):
      #{list.call(host_lines)}
      Servers in here that the host reaches (expose: lines):
      #{list.call(expose_lines)}
      To ask for another host service, add a host: line to .sandbox-mounts,
      e.g. host:127.1.0.2:5432, and ask the user to run `sandbox-agent allow`.
      It takes effect at the next launch. You cannot approve it yourself.
    TXT
  end

  # --- Inside the sandbox (sandbox-network-relay) --------------------------------

  def listen_tcp(ip, port)
    TCPServer.new(ip, port)
  rescue SystemCallError => e
    warn "sandbox-network-relay: cannot listen on #{ip}:#{port}: #{e.message}"
    nil
  end

  def run_relay(config, cmd)
    proxy_socket = config.fetch("proxy_socket")
    expose = config.fetch("expose")

    if (server = listen_tcp(*config.fetch("proxy_listen")))
      serve(server) { |client| splice(client, UNIXSocket.new(proxy_socket)) }
    end

    config.fetch("to_host").each do |ip, port|
      next unless (server = listen_tcp(ip, port))

      serve(server) do |client|
        upstream = UNIXSocket.new(proxy_socket)
        upstream.write("CONNECT #{ip}:#{port} HTTP/1.1\r\nHost: #{ip}:#{port}\r\n\r\n")
        reply = upstream.gets("\r\n\r\n", 65_536).to_s
        reply.start_with?("HTTP/1.1 200") ? splice(client, upstream) : upstream.close
      end
    end

    unless expose.empty?
      serve(UNIXServer.new(config.fetch("expose_socket"))) do |host_side|
        ip, port = host_side.gets.to_s.strip.split(":")
        next unless expose.include?([ip, port.to_i])

        splice(host_side, TCPSocket.new(ip, port.to_i))
      end
    end

    # Handlers, not IGNORE: an ignored signal would be inherited by the command.
    trap("INT") {}
    pid = spawn(*cmd)
    trap("TERM") { Process.kill("TERM", pid) rescue nil }
    _, status = Process.wait2(pid)
    status.exitstatus || 128 + status.termsig.to_i
  end
end
