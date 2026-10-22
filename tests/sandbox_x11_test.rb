#!/usr/bin/env ruby
# Run: ruby tests/sandbox_x11_test.rb

require "minitest/autorun"
require_relative "../sandbox-x11"

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

  def test_numbered_local_record_for_python_xlib
    entry = SandboxX11.xauth_entry("cookie", family: 256, address: "host", number: "12")
    family, rest = entry.unpack("n a*")
    assert_equal 256, family
    fields = []
    4.times do
      len, rest = rest.unpack("n a*")
      fields << rest.byteslice(0, len)
      rest = rest.byteslice(len..)
    end
    assert_equal ["host", "12", "MIT-MAGIC-COOKIE-1", "cookie"], fields
    assert_empty rest
  end
end

class BrowserScaleTest < Minitest::Test
  LAPTOP = "eDP connected primary 2880x1800+0+0 (normal left inverted right x axis y axis) 300mm x 190mm\n"
  MONITOR = "HDMI-1 connected 2560x1440+2880+0 (normal left inverted right x axis y axis) 597mm x 336mm\n"

  def test_a_laptop_panel_alone_draws_at_scale_2
    assert_equal "2", SandboxX11.browser_scale("Screen 0: minimum 320 x 200\n#{LAPTOP}  2880x1800 60.00*+\n")
  end

  def test_an_ordinary_monitor_plugged_in_brings_it_to_1
    assert_equal "1", SandboxX11.browser_scale(LAPTOP + MONITOR)
  end

  def test_a_rotated_panel_keeps_its_density
    assert_equal "2", SandboxX11.browser_scale("eDP connected 1800x2880+0+0 left (normal left inverted right) 300mm x 190mm\n")
  end

  def test_without_a_physical_size_it_says_nothing
    assert_nil SandboxX11.browser_scale("Virtual-1 connected primary 1920x1080+0+0 (normal) 0mm x 0mm\n")
    assert_nil SandboxX11.browser_scale("HDMI-1 disconnected (normal left inverted right x axis y axis)\n")
  end
end
