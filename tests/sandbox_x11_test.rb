#!/usr/bin/env ruby
# Run: ruby tests/sandbox_x11_test.rb

require "minitest/autorun"
require_relative "../sandbox-x11"

class XclipRequestTest < Minitest::Test
  def req(*argv) = SandboxX11.xclip_request(argv)

  # The exact calls Claude Code makes on Ctrl+V.
  def test_claude_code_targets_probe
    assert_equal({ mode: :read, target: "TARGETS", files: [], rmlastnl: false, filter: false },
                 req("-selection", "clipboard", "-t", "TARGETS", "-o"))
  end

  def test_claude_code_image_read
    assert_equal "image/png", req("-selection", "clipboard", "-t", "image/png", "-o")[:target]
  end

  def test_text_read
    r = req("-selection", "clipboard", "-t", "text/plain", "-o")
    assert_equal [:read, "text/plain"], [r[:mode], r[:target]]
  end

  def test_read_defaults_to_utf8_string
    assert_equal "UTF8_STRING", req("-sel", "clip", "-o")[:target]
  end

  def test_write_is_the_default_mode
    r = req("-selection", "clipboard")
    assert_equal [:write, "UTF8_STRING"], [r[:mode], r[:target]]
  end

  def test_write_with_files_and_flags
    r = req("-i", "-sel", "c", "-rmlastnl", "-filter", "notes.txt")
    assert_equal({ mode: :write, target: "UTF8_STRING", files: ["notes.txt"], rmlastnl: true, filter: true }, r)
  end

  def test_abbreviations_like_xclip
    r = req("-sel", "clipboard", "-ta", "image/png", "-out")
    assert_equal [:read, "image/png"], [r[:mode], r[:target]]
  end

  def test_noise_flags_are_accepted
    refute_nil req("-quiet", "-selection", "clipboard", "-o")
    refute_nil req("-selection", "clipboard", "-d", ":0", "-o")
  end

  # PRIMARY stays local: that is where `pass` can be told to put passwords.
  def test_primary_is_not_bridged
    assert_nil req("-o")
    assert_nil req("-selection", "primary", "-o")
    assert_nil req("-selection", "secondary", "-o")
  end

  def test_unknown_or_ambiguous_options_are_refused
    assert_nil req("-selection", "clipboard", "-bogus")
    assert_nil req("-s", "clipboard", "-o")  # -selection, -silent, -sensitive
    assert_nil req("-se", "clipboard", "-o") # -selection, -sensitive
    assert_nil req("-selection")            # missing value
  end

  def test_odd_targets_are_refused
    assert_nil req("-selection", "clipboard", "-t", "image/png\nREAD x", "-o")
    assert_nil req("-selection", "clipboard", "-t", "", "-o")
  end
end

class BridgeRequestTest < Minitest::Test
  def test_read_and_write
    assert_equal [:read, "image/png"], SandboxX11.parse_bridge_request("READ image/png\n")
    assert_equal [:write, "UTF8_STRING"], SandboxX11.parse_bridge_request("WRITE UTF8_STRING\n")
    assert_equal [:read, "text/plain;charset=utf-8"], SandboxX11.parse_bridge_request("READ text/plain;charset=utf-8\n")
  end

  def test_garbage_is_refused
    assert_nil SandboxX11.parse_bridge_request("")
    assert_nil SandboxX11.parse_bridge_request("DELETE x\n")
    assert_nil SandboxX11.parse_bridge_request("READ\n")
    assert_nil SandboxX11.parse_bridge_request("READ a b\n")
    assert_nil SandboxX11.parse_bridge_request("READ $(rm -rf ~)\n")
  end
end

class PickTargetTest < Minitest::Test
  def test_text_wins_over_a_rendered_image
    assert_equal "UTF8_STRING", SandboxX11.pick_target(%w[TARGETS image/png text/html UTF8_STRING STRING])
  end

  def test_returns_the_owner_spelling
    assert_equal "text/plain;charset=UTF-8", SandboxX11.pick_target(%w[text/html text/plain;charset=UTF-8])
  end

  def test_image_when_there_is_no_text
    assert_equal "image/png", SandboxX11.pick_target(%w[TARGETS TIMESTAMP image/png image/bmp])
  end

  def test_nothing_usable
    assert_nil SandboxX11.pick_target(%w[TARGETS TIMESTAMP text/html])
    assert_nil SandboxX11.pick_target([])
  end
end

class LocaluserGrantTest < Minitest::Test
  XHOST = "access control enabled, only authorized clients can connect\nSI:localuser:benjamin\n"

  def test_detects_the_rule
    assert SandboxX11.localuser_grant?(XHOST, "benjamin")
    assert SandboxX11.localuser_grant?(XHOST.upcase, "benjamin")
  end

  def test_other_users_do_not_count
    refute SandboxX11.localuser_grant?(XHOST, "ben")
    refute SandboxX11.localuser_grant?("SI:localuser:root\n", "benjamin")
  end

  def test_access_control_disabled
    assert SandboxX11.access_control_disabled?("access control disabled, clients can connect from any host\n")
    refute SandboxX11.access_control_disabled?(XHOST)
  end
end

class NumberedCookieTest < Minitest::Test
  # `xauth list` as GDM leaves it: no display number, i.e. any display.
  GDM = "pulse/unix:  MIT-MAGIC-COOKIE-1  0123abcd\n#ffff#70756c7365#:  MIT-MAGIC-COOKIE-1  0123abcd\n"

  def test_adds_the_same_cookie_under_the_display_number
    assert_equal ["pulse/unix:0", "MIT-MAGIC-COOKIE-1", "0123abcd"],
                 SandboxX11.missing_numbered_cookie(GDM, "pulse", "0")
  end

  def test_nothing_to_do_once_present
    assert_nil SandboxX11.missing_numbered_cookie(GDM + "pulse/unix:0  MIT-MAGIC-COOKIE-1  0123abcd\n", "pulse", "0")
  end

  def test_nothing_to_copy_from
    assert_nil SandboxX11.missing_numbered_cookie("other/unix:  MIT-MAGIC-COOKIE-1  0123abcd\n", "pulse", "0")
    assert_nil SandboxX11.missing_numbered_cookie("", "pulse", "0")
  end
end

class XauthEntryTest < Minitest::Test
  # Xauthority record: family, address, display number, auth name, auth data —
  # each a big-endian u16 length (family is the u16 itself) then the bytes.
  def test_wildcard_record_layout
    cookie = "\x01".b * 16
    entry = SandboxX11.xauth_wildcard_entry(cookie)
    family, rest = entry.unpack("n a*")
    assert_equal 0xFFFF, family
    fields = []
    4.times do
      len, rest = rest.unpack("n a*")
      fields << rest.byteslice(0, len)
      rest = rest.byteslice(len..)
    end
    assert_equal ["", "", "MIT-MAGIC-COOKIE-1", cookie], fields
    assert_empty rest
  end
end
