#!/usr/bin/env ruby
# Run: ruby tests/sandbox_network_test.rb

require "minitest/autorun"
require "json"
require "tmpdir"
require_relative "../sandbox-network"

class EndpointsTest < Minitest::Test
  def test_single_address_and_port
    assert_equal [["127.1.0.2", 5432]], SandboxNetwork.parse_endpoints("127.1.0.2:5432")
  end

  def test_several_ports
    assert_equal [["127.1.0.1", 3000], ["127.1.0.1", 8080]],
                 SandboxNetwork.parse_endpoints("127.1.0.1:3000,8080")
  end

  # A pool of dev IPs, one per worktree: every host address, every port.
  def test_range_skips_network_and_broadcast
    eps = SandboxNetwork.parse_endpoints("127.5.0.0/30:80,81")
    assert_equal [["127.5.0.1", 80], ["127.5.0.1", 81], ["127.5.0.2", 80], ["127.5.0.2", 81]], eps
  end

  def test_slash_27_gives_thirty_addresses
    assert_equal 30, SandboxNetwork.parse_endpoints("127.5.0.0/27:8000").size
  end

  def test_huge_range_is_refused
    assert_raises(ArgumentError) { SandboxNetwork.parse_endpoints("127.0.0.0/8:80") }
  end

  def test_garbage_is_refused
    ["127.1.0.1", "localhost:80", "127.1.0.1:0", "127.1.0.1:70000", "1.2.3:80"].each do |bad|
      assert_raises(ArgumentError, bad) { SandboxNetwork.parse_endpoints(bad) }
    end
  end
end

class HostListTest < Minitest::Test
  def list(text) = SandboxNetwork::HostList.parse(text)

  def test_a_name_covers_itself_and_its_subdomains
    d = list("example.org\n")
    assert d.match("example.org", [])
    assert d.match("app.example.org", [])
    refute d.match("notexample.org", [])
  end

  def test_leading_wildcard_is_the_same_rule
    assert list("*.example.org").match("example.org", [])
  end

  def test_names_are_case_insensitive
    assert list("Example.ORG").match("APP.example.org", [])
  end

  def test_ip_and_cidr_match_resolved_addresses
    d = list("203.0.113.7\n198.51.100.0/24\n")
    assert d.match("anything", ["203.0.113.7"])
    assert d.match("anything", ["198.51.100.42"])
    refute d.match("anything", ["192.0.2.1"])
  end

  def test_comments_and_blank_lines_are_ignored
    d = list("# docs\n\nexample.org  # the guides\n")
    assert d.match("example.org", [])
  end

  def test_empty_allows_nothing
    refute list("").match("example.org", ["203.0.113.7"])
  end
end

class VerdictTest < Minitest::Test
  def list(text) = SandboxNetwork::HostList.parse(text)

  def verdict(host, port, addrs, to_host: [], allow: "github.com")
    SandboxNetwork.verdict(host, port, addrs, to_host, list(allow))
  end

  def test_allowlisted_host_is_allowed
    assert_equal [:allow, nil], verdict("github.com", 443, ["140.82.121.4"])
  end

  def test_allowlist_covers_subdomains
    assert_equal :allow, verdict("api.github.com", 443, ["140.82.121.6"]).first
  end

  # Restrictive: what is not listed does not go out.
  def test_unlisted_host_is_denied_and_told_how_to_ask
    action, reason = verdict("example.org", 443, ["93.184.215.14"])
    assert_equal :deny, action
    assert_match(/internet:example\.org/, reason)
    assert_match(/sandbox-agent allow/, reason)
  end

  def test_raw_ip_is_denied_unless_listed
    assert_equal :deny, verdict("140.82.121.4", 443, ["140.82.121.4"], allow: "github.com").first
    assert_equal :allow, verdict("140.82.121.4", 443, ["140.82.121.4"], allow: "140.82.121.0/24").first
  end

  # Host loopback is where tunnels and host-only services listen.
  def test_host_loopback_is_denied
    action, reason = verdict("127.0.0.1", 5433, ["127.0.0.1"])
    assert_equal :deny, action
    assert_match(/host:/, reason)
  end

  def test_name_resolving_to_loopback_is_denied
    assert_equal :deny, verdict("sneaky.example.net", 80, ["127.0.0.1"]).first
  end

  def test_unspecified_and_link_local_are_denied
    assert_equal :deny, verdict("0.0.0.0", 80, ["0.0.0.0"]).first
    assert_equal :deny, verdict("169.254.169.254", 80, ["169.254.169.254"]).first
    assert_equal :deny, verdict("::1", 80, ["::1"]).first
  end

  def test_declared_target_is_allowed
    assert_equal :allow, verdict("127.1.0.2", 5432, ["127.1.0.2"], to_host: [["127.1.0.2", 5432]]).first
  end

  def test_declared_address_on_another_port_is_denied
    assert_equal :deny, verdict("127.1.0.2", 22, ["127.1.0.2"], to_host: [["127.1.0.2", 5432]]).first
  end
end

class NetHostsTest < Minitest::Test
  def test_reads_internet_lines_from_mounts_text
    text = "ro:~/x\ninternet:guides.rubyonrails.org\n  internet: *.example.org  # docs\nhost:127.1.0.2:5432\n# internet:commented.org\n"
    assert_equal ["guides.rubyonrails.org", "*.example.org"], SandboxNetwork.internet_hosts(text)
  end
end

class RequestHeadTest < Minitest::Test
  def test_connect
    assert_equal [:connect, "github.com", 443],
                 SandboxNetwork.parse_request_head("CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n")
  end

  def test_connect_ipv6
    assert_equal [:connect, "::1", 443], SandboxNetwork.parse_request_head("CONNECT [::1]:443 HTTP/1.1\r\n\r\n")
  end

  def test_plain_http
    assert_equal [:http, "example.org", 80],
                 SandboxNetwork.parse_request_head("GET http://example.org/x HTTP/1.1\r\nHost: example.org\r\n\r\n")
  end

  def test_origin_form_is_not_a_proxy_request
    assert_nil SandboxNetwork.parse_request_head("GET /x HTTP/1.1\r\n\r\n")
  end

  def test_garbage
    assert_nil SandboxNetwork.parse_request_head("hello\r\n\r\n")
  end
end

# The real thing: bwrap with a private network, the relay inside, the proxy on
# this side. No internet needed — host loopback stands in for both a declared
# host service and an undeclared one.
class PrivateNetworkTest < Minitest::Test
  def setup
    skip "bwrap not installed" unless system("command -v bwrap >/dev/null")
    @dir = Dir.mktmpdir("sandbox-network-test-")
    @log = []
  end

  def teardown
    FileUtils.remove_entry(@dir) if @dir
  end

  def free_port
    s = TCPServer.new("127.0.0.1", 0)
    s.addr[1]
  ensure
    s&.close
  end

  def echo_server
    server = TCPServer.new("127.0.0.1", 0)
    Thread.new { loop { Thread.new(server.accept) { |c| c.write(c.gets.to_s.upcase); c.close } } }
    server.addr[1]
  end

  # Runs `script` with ruby inside a sandbox wired by SandboxNetwork.start.
  def in_sandbox(script, to_host: [], expose: [])
    net = SandboxNetwork.start(@dir, to_host: to_host, expose: expose,
                                 allow: -> { SandboxNetwork::HostList.parse("") },
                                 log: ->(*e) { @log << e })
    args = ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--bind", @dir, @dir, "--unshare-net", "--unshare-pid", "--die-with-parent",
            "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
            *net.env.flat_map { |k, v| ["--setenv", k, v] },
            "--", *net.wrap(["/usr/bin/ruby", "-rsocket", "-e", script])]
    out, status = Open3.capture2("bwrap", *args)
    [out, status]
  ensure
    net&.stop
  end

  def test_declared_host_service_is_reachable
    port = echo_server
    out, status = in_sandbox(<<~RUBY, to_host: [["127.0.0.1", port]])
      s = TCPSocket.new("127.0.0.1", #{port}); s.puts "hello"; print s.gets
    RUBY
    assert status.success?, out
    assert_equal "HELLO\n", out
  end

  def test_undeclared_host_port_is_refused_by_the_proxy
    port = echo_server
    out, _ = in_sandbox(<<~RUBY)
      s = TCPSocket.new("127.0.0.1", 3128)
      s.write "CONNECT 127.0.0.1:#{port} HTTP/1.1\\r\\n\\r\\n"
      print s.gets
    RUBY
    assert_match(/\AHTTP\/1\.1 403/, out)
    assert_equal :deny, @log.last.first
  end

  def test_node_is_told_to_use_the_proxy
    net = SandboxNetwork.start(@dir, to_host: [], expose: [],
                                 allow: -> { SandboxNetwork::HostList.parse("") }, log: ->(*) {})
    assert_equal "1", net.env["NODE_USE_ENV_PROXY"]
  ensure
    net&.stop
  end

  def test_no_direct_route_out
    out, _ = in_sandbox(<<~RUBY)
      begin
        TCPSocket.new("1.1.1.1", 443, connect_timeout: 2); print "connected"
      rescue SystemCallError, IOError => e
        print "refused"
      end
    RUBY
    assert_equal "refused", out
  end

  def test_published_port_is_reachable_from_the_host
    port = free_port
    ready = File.join(@dir, "ready")
    reply = nil
    waiter = Thread.new do
      sleep 0.05 until File.exist?(ready)
      TCPSocket.open("127.0.0.1", port) { |s| s.puts "ping"; reply = s.gets }
    end
    out, status = in_sandbox(<<~RUBY, expose: [["127.0.0.1", port]])
      server = TCPServer.new("127.0.0.1", #{port})
      File.write(#{ready.inspect}, "")
      c = server.accept; c.puts c.gets.to_s.upcase; c.close
    RUBY
    waiter.join(5)
    assert status.success?, out
    assert_equal "PING\n", reply
  end
end
