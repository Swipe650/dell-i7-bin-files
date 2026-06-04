#!/usr/bin/env python3

import gi
gi.require_version('Playerctl', '2.0')
gi.require_version('Gtk', '3.0')
from gi.repository import Playerctl, Gtk, GLib, GdkPixbuf, Gdk, Pango
import urllib.request
import threading
import signal
import psutil

class MusicWidget(Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self, title="Now Playing")
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_app_paintable(True)

        # Transparency using CSS (no deprecation warning)
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
        # The ellipsize mode should come from Pango
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

        # Apply fonts via CSS (no modify_font deprecation)
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

        # Playerctl setup
        self.manager = Playerctl.PlayerManager()
        self.manager.connect("name-appeared", self.on_player_appeared)
        self.manager.connect("player-vanished", self.on_player_vanished)

        players = self.manager.props.player_names
        if players:
            self.on_player_appeared(self.manager, players[0])

        self.connect("realize", self.position_window)
        
                # --- Process check loop to show widget only when Tidal is running ---
        def check_tidal():
            # Check if the Tidal process is running by searching for "tidal" in process names
            tidal_running = any("tidal" in p.name().lower() for p in psutil.process_iter())
            
            # The widget should only be visible when Tidal is running
            if tidal_running:
                if not self.is_visible():
                    self.show_all()  # Show the widget and reposition it
                    self.reposition()
            else:
                if self.is_visible():
                    self.hide()      # Hide the widget
            
            return True  # Keep the timer running

        # Check every 2 seconds
        GLib.timeout_add_seconds(2, check_tidal)
        
        GLib.timeout_add_seconds(1, self.poll_metadata)

    # ---------------------------
    # Playerctl event handlers
    # ---------------------------
    def on_player_appeared(self, manager, player_name):
        if hasattr(self, 'player') and self.player:
            return
        self.player = Playerctl.Player.new_from_name(player_name)
        self.player.connect("metadata", self.on_metadata_change)
        self.poll_metadata()

    def on_player_vanished(self, manager, player_name):
        if hasattr(self, 'player') and self.player:
            self.player = None
            self.artist_value.set_text("No media player found")
            self.title_value.set_text("")
            self.album_value.set_text("")
            self.set_default_image()
            self.reposition()

    def on_metadata_change(self, player, metadata):
        if isinstance(metadata, GLib.Variant):
            metadata = metadata.unpack()
        
        artist_list = metadata.get('xesam:artist', ['Unknown Artist'])
        artist = artist_list[0] if artist_list else 'Unknown Artist'
        title = metadata.get('xesam:title', 'Unknown Title')
        album = metadata.get('xesam:album', 'Unknown Album')
        art_url = metadata.get('mpris:artUrl', None)

        self.artist_value.set_text(artist)
        self.title_value.set_text(title)
        self.album_value.set_text(album)

        if art_url:
            threading.Thread(target=self.load_album_art, args=(art_url,), daemon=True).start()
        else:
            self.set_default_image()

        self.reposition()

    # ---------------------------
    # Album art helpers
    # ---------------------------
    def set_default_image(self):
        # Use a dark grey (or any color) so it's visible
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 64, 64)
        pixbuf.fill(0x444444ff)   # dark grey, alpha=255
        self.image.set_from_pixbuf(pixbuf)

    def load_album_art(self, url):
        if not url:
            GLib.idle_add(self.set_default_image)
            return
        
        if url.startswith('file://'):
            import os
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
        
        # Handle http/https URLs (once)
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

    # ---------------------------
    # Metadata polling fallback
    # ---------------------------
    def poll_metadata(self):
        if hasattr(self, 'player') and self.player:
            try:
                metadata_variant = self.player.get_property('metadata')
                metadata = metadata_variant.unpack() if isinstance(metadata_variant, GLib.Variant) else metadata_variant
                if metadata:
                    self.on_metadata_change(self.player, metadata)
            except Exception as e:
                print(f"Poll error: {e}")
        return True

    # ---------------------------
    # Window positioning with dynamic width and shift
    # ---------------------------
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
            
            # Force a specific width for testing (temporary)
            self.set_size_request(290, -1)
            width, height = self.get_size()  # get again after request
            
            # Calculate x to be flush right with no shift
            x = geometry.x + geometry.width - width
            y = geometry.y + geometry.height - height - 35
                     
            self.set_gravity(Gdk.Gravity.NORTH_EAST)
            self.move(x, y)
            self.get_display().flush()

def main():
    Gtk.init(None)
    widget = MusicWidget()
    widget.show_all()
    signal.signal(signal.SIGINT, lambda sig, frame: Gtk.main_quit())
    Gtk.main()

if __name__ == "__main__":
    main()
