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
#  Helper: get metadata using the playerctl command‑line tool
# ----------------------------------------------------------------------
def get_playerctl_metadata():
    try:
        out = subprocess.check_output(
            ['playerctl', '-i', 'plasma-browser-integration', 'metadata', '--format',
             '{{ title }}||{{ artist }}||{{ album }}||{{ mpris:artUrl }}'],
            stderr=subprocess.DEVNULL,
            timeout=1
        ).decode().strip()
        if out and '||' in out:
            title, artist, album, art_url = out.split('||', 3)
            return ('' if title == 'null' else title,
                    '' if artist == 'null' else artist,
                    '' if album == 'null' else album,
                    '' if art_url == 'null' else art_url)
    except:
        pass
    return ('', '', '', '')


class MusicWidget(Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self, title="Now Playing")
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_app_paintable(True)
        self.set_keep_below(True)

        # Fixed size (the KWin rule uses these dimensions)
        self.set_default_size(290, 180)
        self.set_size_request(290, 180)

        # Start hidden
        self.hide()

        # Transparency
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

        # Album art
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

        # Title row (ellipsis)
        self.title_header = Gtk.Label()
        self.title_header.set_markup("<b>Title:</b>")
        self.title_header.set_halign(Gtk.Align.START)
        text_grid.attach(self.title_header, 0, 0, 1, 1)

        self.title_value = Gtk.Label()
        self.title_value.set_halign(Gtk.Align.START)
        self.title_value.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_value.set_text("Unknown Title")
        text_grid.attach(self.title_value, 1, 0, 1, 1)

        # Artist row (ellipsis)
        self.artist_header = Gtk.Label()
        self.artist_header.set_markup("<b>Artist:</b>")
        self.artist_header.set_halign(Gtk.Align.START)
        text_grid.attach(self.artist_header, 0, 1, 1, 1)

        self.artist_value = Gtk.Label()
        self.artist_value.set_halign(Gtk.Align.START)
        self.artist_value.set_ellipsize(Pango.EllipsizeMode.END)
        self.artist_value.set_text("Unknown Artist")
        text_grid.attach(self.artist_value, 1, 1, 1, 1)

        # Album row (wraps, top‑aligned)
        self.album_header = Gtk.Label()
        self.album_header.set_markup("<b>Album:</b>")
        self.album_header.set_halign(Gtk.Align.START)
        self.album_header.set_valign(Gtk.Align.START)
        text_grid.attach(self.album_header, 0, 2, 1, 1)

        self.album_value = Gtk.Label()
        self.album_value.set_halign(Gtk.Align.START)
        self.album_value.set_valign(Gtk.Align.START)
        self.album_value.set_line_wrap(True)
        self.album_value.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.album_value.set_max_width_chars(35)
        self.album_value.set_margin_start(0)      # remove any left margin
        self.album_value.set_margin_end(0)        # remove any right margin
        self.album_value.set_xalign(0.0)          # force text to left edge
        self.album_value.set_text("Unknown Album")
        text_grid.attach(self.album_value, 1, 2, 1, 1)

        # Fonts
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

        self.connect("realize", self.position_window)

        self.last_track = ""

        # --- Tidal detection (show/hide) ---
        def check_tidal():
            tidal_running = any("tidal" in p.name().lower() for p in psutil.process_iter())
            if tidal_running:
                if not self.is_visible():
                    self.show_all()
            else:
                if self.is_visible():
                    self.hide()
            return True

        GLib.timeout_add_seconds(2, check_tidal)
        GLib.timeout_add_seconds(2, self.poll_metadata)

    def poll_metadata(self):
        if not self.is_visible():
            return True

        title, artist, album, art_url = get_playerctl_metadata()
        track_id = f"{title}|{artist}|{album}"

        if track_id != self.last_track:
            self.last_track = track_id
            # Update text immediately
            self.title_value.set_text(title if title else "Unknown Title")
            self.artist_value.set_text(artist if artist else "Unknown Artist")
            self.album_value.set_text(album if album else "Unknown Album")

            # Schedule retries for album art
            def try_load_art(retry_count=0):
                if retry_count >= 3:
                    print("Failed to load album art after 3 attempts. Using default.")
                    self.set_default_image()
                    return
                # Re-fetch the metadata to get the art_url
                _, _, _, current_art_url = get_playerctl_metadata()
                if current_art_url:
                    print(f"Retry {retry_count+1}: Art URL found, loading...")
                    threading.Thread(target=self.load_album_art,
                                     args=(current_art_url,),
                                     daemon=True).start()
                else:
                    print(f"Retry {retry_count+1}: Art URL not found, retrying...")
                    GLib.timeout_add_seconds(1, lambda: try_load_art(retry_count + 1))

            # Start the first attempt with the original art_url
            if art_url:
                threading.Thread(target=self.load_album_art,
                                 args=(art_url,),
                                 daemon=True).start()
            else:
                print("Initial art_url missing. Starting fallback retry loop.")
                GLib.timeout_add_seconds(1, try_load_art)

        return True

    # ------------------------------------------------------------------
    #  Album art helpers
    # ------------------------------------------------------------------
    def set_default_image(self):
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 64, 64)
        pixbuf.fill(0x444444ff)
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
    #  Window positioning (no reposition needed – KWin rule handles it)
    # ------------------------------------------------------------------
    def position_window(self, _widget):
        # The KWin rule forces the correct position; nothing to do here.
        pass


def main():
    Gtk.init(None)
    widget = MusicWidget()
    signal.signal(signal.SIGINT, lambda sig, frame: Gtk.main_quit())
    Gtk.main()

if __name__ == "__main__":
    main()
