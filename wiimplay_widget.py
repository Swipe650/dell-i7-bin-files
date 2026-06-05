#!/usr/bin/env python3

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf, Pango
import urllib.request
import threading
import signal
import psutil
import subprocess
import os

# ----------------------------------------------------------------------
#  Helper: get metadata from wiimplay using playerctl -p wiimplay
# ----------------------------------------------------------------------
def get_wiimplay_metadata():
    """
    Returns a tuple: (title, artist, album, art_url)
    If wiimplay is not running or metadata is missing, returns ('', '', '', '')
    """
    try:
        # Use -p wiimplay to target that specific player
        out = subprocess.check_output(
            ['playerctl', '-p', 'wiimplay', 'metadata', '--format',
             '{{ title }}||{{ artist }}||{{ album }}||{{ mpris:artUrl }}'],
            stderr=subprocess.DEVNULL,
            timeout=1
        ).decode().strip()
        if out and '||' in out:
            title, artist, album, art_url = out.split('||', 3)
            # playerctl prints "null" for missing values
            return ('' if title == 'null' else title,
                    '' if artist == 'null' else artist,
                    '' if album == 'null' else album,
                    '' if art_url == 'null' else art_url)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError):
        pass
    return ('', '', '', '')


class WiimplayWidget(Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self, title="Wiimplay Now Playing")
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_app_paintable(True)

        # Keep below other windows (background widget)
        self.set_keep_below(True)

        # Transparency using CSS
        self.set_visual(self.get_screen().get_rgba_visual())
        css_opacity = b"window { opacity: 0.95; }"
        opacity_provider = Gtk.CssProvider()
        opacity_provider.load_from_data(css_opacity)
        self.get_style_context().add_provider(opacity_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Main horizontal box
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        main_box.set_margin_top(8)
        main_box.set_margin_bottom(8)
        main_box.set_margin_start(8)
        main_box.set_margin_end(8)
        self.add(main_box)

        # Album art - align to top
        self.image = Gtk.Image()
        self.image.set_size_request(64, 64)
        self.image.set_valign(Gtk.Align.START)
        main_box.pack_start(self.image, False, False, 0)

        # Grid for text
        text_grid = Gtk.Grid()
        text_grid.set_column_spacing(8)
        text_grid.set_row_spacing(4)
        text_grid.set_hexpand(True)
        main_box.pack_start(text_grid, True, True, 0)

        # Title row
        self.title_header = Gtk.Label()
        self.title_header.set_markup("<b>Title:</b>")
        self.title_header.set_halign(Gtk.Align.START)
        text_grid.attach(self.title_header, 0, 0, 1, 1)

        self.title_value = Gtk.Label()
        self.title_value.set_halign(Gtk.Align.START)
        self.title_value.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_value.set_text("Unknown Title")
        text_grid.attach(self.title_value, 1, 0, 1, 1)

        # Artist row
        self.artist_header = Gtk.Label()
        self.artist_header.set_markup("<b>Artist:</b>")
        self.artist_header.set_halign(Gtk.Align.START)
        text_grid.attach(self.artist_header, 0, 1, 1, 1)

        self.artist_value = Gtk.Label()
        self.artist_value.set_halign(Gtk.Align.START)
        self.artist_value.set_ellipsize(Pango.EllipsizeMode.END)
        self.artist_value.set_text("Unknown Artist")
        text_grid.attach(self.artist_value, 1, 1, 1, 1)

        # Album row
        self.album_header = Gtk.Label()
        self.album_header.set_markup("<b>Album:</b>")
        self.album_header.set_halign(Gtk.Align.START)
        text_grid.attach(self.album_header, 0, 2, 1, 1)

        self.album_value = Gtk.Label()
        self.album_value.set_halign(Gtk.Align.START)
        self.album_value.set_ellipsize(Pango.EllipsizeMode.END)
        self.album_value.set_text("Unknown Album")
        text_grid.attach(self.album_value, 1, 2, 1, 1)

        # Apply fonts via CSS
        css_font = b"""
            .header { font-size: 9pt; font-weight: bold; }
            .value { font-size: 10pt; }
        """
        font_provider = Gtk.CssProvider()
        font_provider.load_from_data(css_font)
        self.get_style_context().add_provider(font_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        for header in [self.title_header, self.artist_header, self.album_header]:
            header.get_style_context().add_class("header")
        for value in [self.title_value, self.artist_value, self.album_value]:
            value.get_style_context().add_class("value")

        self.set_default_image()

        # Position window (bottom‑right)
        self.connect("realize", self.position_window)

        # Store the last known track to avoid unnecessary UI updates
        self.last_track = ""

        # --- Process check loop: show widget only when wiimplay is running ---
        def check_wiimplay():
            # Look for a process named 'wiimplay' (adjust if the binary name differs)
            wiimplay_running = any("wiimplay" in p.name().lower() for p in psutil.process_iter())
            if wiimplay_running:
                if not self.is_visible():
                    self.show_all()
                    self.reposition()
            else:
                if self.is_visible():
                    self.hide()
            return True

        GLib.timeout_add_seconds(2, check_wiimplay)

        # --- Polling for metadata (only when visible) ---
        GLib.timeout_add_seconds(2, self.poll_metadata)

    # ------------------------------------------------------------------
    #  Helper: update UI from playerctl -p wiimplay command
    # ------------------------------------------------------------------
    def poll_metadata(self):
        if not self.is_visible():
            return True

        title, artist, album, art_url = get_wiimplay_metadata()

        track_id = f"{title}|{artist}|{album}"

        if track_id != self.last_track:
            self.last_track = track_id

            self.title_value.set_text(title if title else "Unknown Title")
            self.artist_value.set_text(artist if artist else "Unknown Artist")
            self.album_value.set_text(album if album else "Unknown Album")

            if art_url:
                threading.Thread(target=self.load_album_art, args=(art_url,), daemon=True).start()
            else:
                self.set_default_image()

            self.reposition()

        return True

    # ------------------------------------------------------------------
    #  Album art helpers
    # ------------------------------------------------------------------
    def set_default_image(self):
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 64, 64)
        pixbuf.fill(0x444444ff)   # dark grey
        self.image.set_from_pixbuf(pixbuf)

    def load_album_art(self, url):
        if not url:
            GLib.idle_add(self.set_default_image)
            return

        if url.startswith('file://'):
            path = url[7:]
            try:
                if os.path.exists(path) and os.path.isfile(path):
                    with open(path, 'rb') as f:
                        data = f.read()
                    loader = GdkPixbuf.PixbufLoader.new()
                    loader.write(data)
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    scaled = pixbuf.scale_simple(64, 64, GdkPixbuf.InterpType.BILINEAR)
                    GLib.idle_add(self.image.set_from_pixbuf, scaled)
                    return
            except Exception:
                pass
            GLib.idle_add(self.set_default_image)
            return

        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                data = response.read()
            loader = GdkPixbuf.PixbufLoader.new()
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()
            scaled = pixbuf.scale_simple(64, 64, GdkPixbuf.InterpType.BILINEAR)
            GLib.idle_add(self.image.set_from_pixbuf, scaled)
        except Exception:
            GLib.idle_add(self.set_default_image)

    # ------------------------------------------------------------------
    #  Window positioning (flush right, gap_y=35)
    # ------------------------------------------------------------------
    def position_window(self, _widget):
        self.reposition()

    def reposition(self):
        display = Gdk.Display.get_default()
        if display:
            monitor = display.get_primary_monitor()
            if not monitor:
                monitor = display.get_monitor(0)
            geometry = monitor.get_geometry()
            width, height = self.get_size()

            self.set_size_request(290, -1)
            width, height = self.get_size()

            x = geometry.x + geometry.width - width
            y = geometry.y + geometry.height - height - 35

            self.set_gravity(Gdk.Gravity.NORTH_EAST)
            self.move(x, y)
            self.get_display().flush()


def main():
    Gtk.init(None)
    widget = WiimplayWidget()
    widget.show_all()
    signal.signal(signal.SIGINT, lambda sig, frame: Gtk.main_quit())
    Gtk.main()

if __name__ == "__main__":
    main()
